# src/execution/position_monitor_agent.py
# "The Watcher" — tracks every open position in real time.
# Trails stops as trades move in favor, triggers exits when conditions are met.
# Never stops watching.

import asyncio
import json
import os
import time
from datetime import time as dtime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.shared.base_agent import BaseAgent
from src.shared.constants import CH, SK, AGENTS

load_dotenv()


def _load_config() -> dict:
    config_path = Path(__file__).resolve().parents[2] / "config" / "exit-rules.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


class PositionMonitorAgent(BaseAgent):
    """The Watcher — hyper-vigilant sentinel for open positions."""

    def __init__(self):
        super().__init__(AGENTS.WATCHER)
        self.cfg = _load_config()

        # Trailing stop: activate at X% gain, trail at Y% behind price
        self.trailing_stop_activation_pct: float = float(self.cfg.get("trailing_stop_activation_pct", 0.5))
        self.trailing_stop_trail_pct: float = float(self.cfg.get("trailing_stop_trail_pct", 0.25))
        self.use_target_exit: bool = bool(self.cfg.get("use_target_exit", False))
        self.time_exit_minutes: int = int(self.cfg.get("time_exit_minutes", 30))
        self.poll_interval_s: float = float(self.cfg.get("poll_interval_s", 1.0))

        # Live position mirror: ticker -> position dict
        self.positions: dict[str, dict] = {}

        # Prevent double-exiting the same ticker
        self.exiting: set[str] = set()

        # Last known ATR per ticker (populated from market data packets)
        self.atr_cache: dict[str, float] = {}

        # Latest price per ticker
        self.price_cache: dict[str, float] = {}

        # Flag set on daily halt
        self.halted: bool = False

        # End-of-day force liquidation time (3:45 PM ET — 15 min before close)
        self.eod_liquidation_time: dtime = dtime(15, 45)
        self.eod_liquidated: bool = False

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        await self.subscribe(CH.FILL_REPORTS, CH.MARKET_DATA, CH.DAILY_HALT)
        self.log.info("watcher_ready", subscriptions=[CH.FILL_REPORTS, CH.MARKET_DATA, CH.DAILY_HALT])

        # Background poll loop for time-exits (runs even without new market data)
        asyncio.create_task(self._poll_loop())

        async for msg in self.listen():
            if not self.running:
                break
            try:
                await self._route_message(msg)
            except Exception as e:
                self.log.error("watcher_msg_error", error=str(e))

    async def _route_message(self, msg: dict):
        # Detect message type by content fields
        if "exit_type" in msg:
            # This is our own exit order bouncing back — ignore
            return
        if "halted" in msg or msg.get("agent") == "actuary":
            # Daily halt signal
            await self._on_daily_halt()
        elif "is_exit" in msg:
            # Fill report from The Sniper
            await self._on_fill_report(msg)
        elif "bid" in msg and "ask" in msg:
            # Market data packet from The Scanner
            await self._on_market_data(msg)

    # ── Fill report handling ──────────────────────────────────────────────────

    async def _on_fill_report(self, fill: dict):
        ticker = fill.get("ticker", "")
        is_exit = fill.get("is_exit", False)

        if not ticker:
            return

        if not is_exit:
            # Entry fill — load position from Redis state (Sniper wrote it)
            pos = await self.state_hget(SK.POSITIONS, ticker)
            if pos:
                self.positions[ticker] = pos
                self.log.info(
                    "watcher_tracking",
                    ticker=ticker,
                    direction=pos.get("direction"),
                    entry=pos.get("entry_price"),
                    stop=pos.get("stop_price"),
                    target=pos.get("target_price"),
                )
        else:
            # Exit confirmed — stop tracking
            self.positions.pop(ticker, None)
            self.exiting.discard(ticker)
            self.log.info("watcher_position_closed", ticker=ticker, exit_type=fill.get("exit_type"))

    # ── Market data handling ──────────────────────────────────────────────────

    async def _on_market_data(self, packet: dict):
        ticker = packet.get("ticker", "")
        if not ticker:
            return

        # Update price cache — use midpoint of bid/ask
        bid = float(packet.get("bid", 0))
        ask = float(packet.get("ask", 0))
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2
        else:
            mid = float(packet.get("last", 0))

        if mid > 0:
            self.price_cache[ticker] = mid

        # Only care about positions we're tracking
        if ticker not in self.positions or ticker in self.exiting:
            return

        pos = self.positions[ticker]
        current_price = mid if mid > 0 else float(pos.get("entry_price", 0))

        await self._evaluate_position(ticker, pos, current_price, packet)

    async def _evaluate_position(self, ticker: str, pos: dict, current_price: float, packet: dict):
        direction = pos.get("direction", "long")
        entry_price = float(pos.get("entry_price", 0))
        stop_price = float(pos.get("stop_price", 0))
        target_price = float(pos.get("target_price", 0))
        shares = int(pos.get("shares", 0))
        entry_time_ms = int(pos.get("entry_time_ms", 0))
        trailing_stop = pos.get("trailing_stop")  # None or float

        if shares <= 0 or entry_price <= 0:
            return

        is_long = direction == "long"

        # ── 1. Time exit check ────────────────────────────────────────────────
        if self.time_exit_minutes > 0:
            now_ms = await self.now_ms()
            age_minutes = (now_ms - entry_time_ms) / 60_000
            if age_minutes >= self.time_exit_minutes:
                await self._trigger_exit(ticker, pos, "time_exit",
                                         f"Position exceeded {self.time_exit_minutes}min time limit",
                                         current_price)
                return

        # ── 2. Target hit (optional — disabled by default to let winners run)
        if self.use_target_exit and target_price > 0:
            if is_long and current_price >= target_price:
                await self._trigger_exit(ticker, pos, "target_hit",
                                         f"Target {target_price:.2f} reached at {current_price:.2f}",
                                         current_price)
                return
            elif not is_long and current_price <= target_price:
                await self._trigger_exit(ticker, pos, "target_hit",
                                         f"Target {target_price:.2f} reached at {current_price:.2f}",
                                         current_price)
                return

        # ── 3. Percentage-based trailing stop ─────────────────────────────────
        # Read live trade rules from Redis (UI overrides)
        try:
            ui_trail_act = await self.state_hget("state:trade-rules", "trailing_stop_activation_pct")
            ui_trail_pct = await self.state_hget("state:trade-rules", "trailing_stop_trail_pct")
            ui_time_exit = await self.state_hget("state:trade-rules", "time_exit_minutes")
            ui_use_target = await self.state_hget("state:trade-rules", "use_target_exit")
            if ui_trail_act is not None:
                self.trailing_stop_activation_pct = float(ui_trail_act)
            if ui_trail_pct is not None:
                self.trailing_stop_trail_pct = float(ui_trail_pct)
            if ui_time_exit is not None:
                self.time_exit_minutes = int(float(ui_time_exit))
            if ui_use_target is not None:
                self.use_target_exit = str(ui_use_target).lower() in ("true", "1", "yes")
        except Exception:
            pass

        # Calculate % gain from entry
        if is_long:
            gain_pct = ((current_price - entry_price) / entry_price) * 100
        else:
            gain_pct = ((entry_price - current_price) / entry_price) * 100

        # Activate trailing stop once gain exceeds activation threshold
        if gain_pct >= self.trailing_stop_activation_pct:
            trail_distance = current_price * (self.trailing_stop_trail_pct / 100)

            if is_long:
                new_trail = current_price - trail_distance
                if trailing_stop is None or new_trail > trailing_stop:
                    trailing_stop = new_trail
                    pos["trailing_stop"] = trailing_stop
                    self.positions[ticker] = pos
                    await self.state_hset(SK.POSITIONS, ticker, pos)
                    self.log.info("watcher_trailing_stop_updated",
                                  ticker=ticker, trailing_stop=round(trailing_stop, 4),
                                  gain_pct=round(gain_pct, 3))
            else:
                new_trail = current_price + trail_distance
                if trailing_stop is None or new_trail < trailing_stop:
                    trailing_stop = new_trail
                    pos["trailing_stop"] = trailing_stop
                    self.positions[ticker] = pos
                    await self.state_hset(SK.POSITIONS, ticker, pos)
                    self.log.info("watcher_trailing_stop_updated",
                                  ticker=ticker, trailing_stop=round(trailing_stop, 4),
                                  gain_pct=round(gain_pct, 3))

        # ── 4. Trailing stop hit ──────────────────────────────────────────────
        if trailing_stop is not None:
            if is_long and current_price <= trailing_stop:
                await self._trigger_exit(ticker, pos, "trailing_stop_triggered",
                                         f"Price {current_price:.2f} dropped below trailing stop {trailing_stop:.2f} (gain was {gain_pct:.2f}%)",
                                         current_price)
                return
            elif not is_long and current_price >= trailing_stop:
                await self._trigger_exit(ticker, pos, "trailing_stop_triggered",
                                         f"Price {current_price:.2f} rose above trailing stop {trailing_stop:.2f} (gain was {gain_pct:.2f}%)",
                                         current_price)
                return

        # ── 5. Hard stop hit ─────────────────────────────────────────────────
        if stop_price > 0:
            if is_long and current_price <= stop_price:
                await self._trigger_exit(ticker, pos, "stop_hit",
                                         f"Price {current_price:.2f} hit hard stop {stop_price:.2f}",
                                         current_price)
                return
            elif not is_long and current_price >= stop_price:
                await self._trigger_exit(ticker, pos, "stop_hit",
                                         f"Price {current_price:.2f} hit hard stop {stop_price:.2f}",
                                         current_price)
                return

    # ── Daily halt handling ───────────────────────────────────────────────────

    async def _on_daily_halt(self):
        if self.halted:
            return
        self.halted = True
        self.log.warning("watcher_daily_halt_received — exiting all positions immediately")

        for ticker, pos in list(self.positions.items()):
            current_price = self.price_cache.get(ticker, float(pos.get("entry_price", 0)))
            await self._trigger_exit(ticker, pos, "risk_halt",
                                     "Daily halt triggered by The Actuary", current_price)

    # ── Poll loop (time exits) ────────────────────────────────────────────────

    async def _poll_loop(self):
        """Secondary loop that catches time exits and end-of-day liquidation."""
        while self.running:
            await asyncio.sleep(self.poll_interval_s)

            # ── End-of-day force liquidation (3:45 PM ET) ──────────────────
            if not self.eod_liquidated and self.positions:
                try:
                    now_et = await self.now_et()
                    now_time = now_et.time().replace(tzinfo=None)
                    if now_time >= self.eod_liquidation_time:
                        self.eod_liquidated = True
                        self.log.warning("eod_liquidation_triggered",
                                         time=now_time.strftime("%H:%M"),
                                         positions=len(self.positions))
                        for ticker, pos in list(self.positions.items()):
                            if ticker in self.exiting:
                                continue
                            current_price = self.price_cache.get(ticker, float(pos.get("entry_price", 0)))
                            await self._trigger_exit(ticker, pos, "eod_liquidation",
                                                     f"End-of-day forced exit at {now_time.strftime('%H:%M')} ET",
                                                     current_price)
                        continue  # Skip time-exit checks this iteration
                except Exception as exc:
                    self.log.error("eod_check_error", error=str(exc))

            # ── Time exit checks ───────────────────────────────────────────
            now_ms = await self.now_ms()
            for ticker, pos in list(self.positions.items()):
                if ticker in self.exiting:
                    continue
                entry_time_ms = int(pos.get("entry_time_ms", 0))
                age_minutes = (now_ms - entry_time_ms) / 60_000
                if age_minutes >= self.time_exit_minutes:
                    current_price = self.price_cache.get(ticker, float(pos.get("entry_price", 0)))
                    await self._trigger_exit(ticker, pos, "time_exit",
                                             f"Time exit: position open {age_minutes:.1f}min",
                                             current_price)

    # ── Exit trigger ─────────────────────────────────────────────────────────

    async def _trigger_exit(self, ticker: str, pos: dict, exit_type: str, reason: str, approx_price: float):
        if ticker in self.exiting:
            return  # Already being exited

        self.exiting.add(ticker)
        shares = int(pos.get("shares", 0))
        directive_id = pos.get("directive_id", self.make_id())

        self.log.info(
            "watcher_exit_triggered",
            ticker=ticker,
            exit_type=exit_type,
            reason=reason,
            shares=shares,
            approx_price=approx_price,
        )

        exit_order = {
            "directive_id": directive_id,
            "ticker": ticker,
            "setup_type": pos.get("setup_type", "unknown"),
            "direction": pos.get("direction", "long"),
            "entry_price": float(pos.get("entry_price", 0)),
            "exit_type": exit_type,
            "shares": shares,
            "order_type": "market",
            "reason": reason,
            "exit_price_approx": round(approx_price, 4),
        }
        await self.publish(CH.EXIT_ORDERS, exit_order)

    # ── ATR helper ────────────────────────────────────────────────────────────

    def _get_atr(self, ticker: str, entry_price: float) -> float:
        """Return cached ATR or fallback to 0.5% of entry price."""
        return self.atr_cache.get(ticker, entry_price * 0.005)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    agent = PositionMonitorAgent()
    try:
        await agent.start()
    except KeyboardInterrupt:
        await agent.stop()


if __name__ == "__main__":
    asyncio.run(main())
