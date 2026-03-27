# src/agents/risk_modeling_agent.py
# "The Actuary" — Gates every setup through risk rules, sizes positions using
# ATR-based heat limits, monitors daily P&L, and triggers the session halt.

import asyncio
import json
import os
import time
from typing import Optional

import yaml
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from src.shared.base_agent import BaseAgent
from src.shared.constants import AGENTS, CH, SK, SETUP

load_dotenv()

# ── Config loader ─────────────────────────────────────────────────────────────

def _load_config() -> dict:
    with open("config/risk-params.yaml", "r") as fh:
        return yaml.safe_load(fh)


# ── Agent ─────────────────────────────────────────────────────────────────────

class RiskModelingAgent(BaseAgent):
    """The Actuary — approves or vetoes setups and enforces daily halt rules."""

    def __init__(self):
        super().__init__(AGENTS.ACTUARY)
        cfg = _load_config()

        # Risk parameters from config
        self.daily_drawdown_halt_pct: float  = float(cfg.get("daily_drawdown_halt_pct", 3.0))
        self.max_position_heat_pct: float    = float(cfg.get("max_position_heat_pct", 2.0))
        self.max_portfolio_heat_pct: float   = float(cfg.get("max_portfolio_heat_pct", 4.0))
        self.min_rr_ratio: float             = float(cfg.get("min_rr_ratio", 2.0))
        self.min_quality_score: int          = int(cfg.get("min_quality_score", 6))
        self.vix_halt_level: float           = float(cfg.get("vix_halt_level", 35.0))
        self.sizing_method: str              = str(cfg.get("sizing_method", "atr"))
        self.atr_stop_multiplier: float      = float(cfg.get("atr_stop_multiplier", 1.5))
        self.kelly_cap: float                = float(cfg.get("kelly_cap", 0.25))
        self.kelly_min_samples: int          = int(cfg.get("kelly_min_samples", 20))

        # Hard guardrails — prevents runaway position accumulation
        self.max_open_positions: int         = int(cfg.get("max_open_positions", 4))
        self.max_total_notional_pct: float   = float(cfg.get("max_total_notional_pct", 60.0))

        # Account value
        self.account_value: float = float(os.getenv("ACCOUNT_VALUE", "25000"))

        # Alpaca client for checking real positions
        _key = os.getenv("ALPACA_API_KEY", "")
        _secret = os.getenv("ALPACA_SECRET_KEY", "")
        _paper = "paper" in os.getenv("ALPACA_BASE_URL", "").lower()
        self._alpaca = TradingClient(api_key=_key, secret_key=_secret, paper=_paper) if _key and _secret else None

        # In-memory position cache: ticker -> position dict
        # Refreshed from Redis on every setup evaluation, updated on fill reports
        self._positions: dict[str, dict] = {}
        # Alpaca position tickers — synced periodically
        self._alpaca_tickers: set[str] = set()

    # ── Position state helpers ────────────────────────────────────────────────

    async def _sync_alpaca_positions(self) -> None:
        """Fetch real positions from Alpaca so we never double-enter."""
        if not self._alpaca:
            return
        try:
            client = self._alpaca
            alpaca_positions = await asyncio.to_thread(lambda: client.get_all_positions())  # type: ignore[union-attr]
            self._alpaca_tickers = {p.symbol for p in alpaca_positions}
        except Exception as e:
            self.log.warning("alpaca_position_sync_failed", error=str(e))

    async def _load_positions(self) -> dict[str, dict]:
        """Read current open positions from the state store."""
        raw = await self.state_hgetall(SK.POSITIONS)
        positions = {}
        for ticker, val in raw.items():
            if isinstance(val, dict):
                positions[ticker] = val
        self._positions = positions
        return positions

    async def _save_position(self, ticker: str, position: dict) -> None:
        """Persist a single position back to the state store."""
        await self.state_hset(SK.POSITIONS, ticker, position)
        self._positions[ticker] = position

    async def _remove_position(self, ticker: str) -> None:
        """Remove a closed position from the state store."""
        await self.redis.hdel(SK.POSITIONS, ticker)
        self._positions.pop(ticker, None)

    # ── Portfolio heat calculation ────────────────────────────────────────────

    def _current_portfolio_heat_pct(self, positions: dict[str, dict]) -> float:
        """
        Sum of (max_loss_dollars / account_value) * 100 across all open positions.
        """
        if self.account_value <= 0:
            return 0.0
        total_risk_dollars = sum(
            float(p.get("max_loss_dollars", 0.0)) for p in positions.values()
        )
        return (total_risk_dollars / self.account_value) * 100.0

    # ── Position sizing ───────────────────────────────────────────────────────

    async def _size_position(self, entry_price: float, stop_price: float) -> tuple[int, float]:
        """
        ATR-based sizing: allocate at most max_position_heat_pct of account per trade.

        Returns (shares, max_loss_dollars).
        """
        stop_distance = abs(entry_price - stop_price)

        # Guard: if stop is missing or too tight, use 0.5% of entry as minimum
        min_stop = entry_price * 0.005
        if stop_distance < min_stop:
            stop_distance = min_stop

        # Check Redis for live UI override of max loss per trade
        heat_pct = self.max_position_heat_pct
        try:
            ui_heat = await self.state_hget("state:trade-rules", "max_loss_per_trade_pct")
            if ui_heat is not None:
                heat_pct = float(ui_heat)
        except Exception:
            pass
        max_risk_dollars = self.account_value * (heat_pct / 100.0)
        shares = int(max_risk_dollars / stop_distance)

        # Hard cap: never more than 10,000 shares to prevent runaway sizing
        shares = min(shares, 10_000)

        # Notional cap: check Redis for live UI override, fall back to 20% of account
        max_notional = self.account_value * 0.20
        try:
            ui_max = await self.state_hget("state:trade-rules", "max_position_dollars")
            if ui_max is not None:
                max_notional = float(ui_max)
        except Exception:
            pass
        if entry_price > 0:
            max_shares_by_notional = int(max_notional / entry_price)
            if shares > max_shares_by_notional:
                shares = max_shares_by_notional

        max_loss_dollars = round(shares * stop_distance, 2)
        return shares, max_loss_dollars


    # ── R/R calculation ───────────────────────────────────────────────────────

    def _calc_rr(self, entry: float, stop: float, target: float, direction: str) -> float:
        """Calculate risk/reward ratio for a setup."""
        stop_dist   = abs(entry - stop)
        target_dist = abs(entry - target)
        if stop_dist <= 0:
            return 0.0
        return round(target_dist / stop_dist, 3)

    # ── Setup evaluation ──────────────────────────────────────────────────────

    async def _evaluate_setup(self, setup: dict) -> None:
        """
        Run every risk check and publish either an approved or vetoed decision
        to CH.RISK_DECISIONS.
        """
        setup_id    = setup.get("setup_id", self.make_id())
        ticker      = setup.get("ticker", "")
        quality     = int(setup.get("quality_score", 0))
        direction   = setup.get("direction", "long")
        entry_zone  = setup.get("entry_zone", [0.0, 0.0])
        stop_price  = float(setup.get("suggested_stop", 0.0))
        target      = float(setup.get("suggested_target", 0.0))

        # Skip diagnostic packets — not real setups
        setup_type = setup.get("setup_type", "")
        if setup_type in ("warming_up", "indicator_snapshot", "__watchlist__"):
            return

        # Compute entry mid from entry_zone
        if isinstance(entry_zone, list) and len(entry_zone) == 2:
            entry_price = (float(entry_zone[0]) + float(entry_zone[1])) / 2.0
        else:
            entry_price = float(entry_zone[0]) if entry_zone else 0.0

        # ── Gate 1: halt check (sacred — always first) ────────────────────
        if await self.is_halted():
            await self._veto(setup_id, "session_halted",
                             {"current_heat_pct": None, "max_heat_pct": None}, setup_type=setup_type)
            return

        # ── Gate 2: quality score ─────────────────────────────────────────
        if quality < self.min_quality_score:
            await self._veto(setup_id, "quality_below_minimum",
                             {"quality_score": quality, "min_required": self.min_quality_score}, setup_type=setup_type)
            return


        # ── Gate 3: load current positions (Redis + Alpaca) ───────────────
        positions = await self._load_positions()
        await self._sync_alpaca_positions()

        # ── Gate 4: duplicate position check (Redis OR Alpaca) ─────────
        if ticker in positions or ticker in self._alpaca_tickers:
            await self._veto(setup_id, "position_already_open",
                             {"ticker": ticker}, setup_type=setup_type)
            return

        # ── Gate 4a: max open positions (HARD CAP) ─────────────────────
        # Check Redis for live UI override
        max_pos = self.max_open_positions
        try:
            ui_max_pos = await self.state_hget("state:trade-rules", "max_open_positions")
            if ui_max_pos is not None:
                max_pos = int(float(ui_max_pos))
        except Exception:
            pass
        if len(positions) >= max_pos:
            await self._veto(setup_id, "max_open_positions_reached",
                             {"open_positions": len(positions), "max_allowed": max_pos},
                             setup_type=setup_type)
            return

        # ── Gate 4b: max total notional exposure (HARD CAP) ────────────
        total_notional = sum(
            float(p.get("entry_price", 0)) * int(p.get("shares", 0))
            for p in positions.values()
        )
        max_notional_total = self.account_value * (self.max_total_notional_pct / 100.0)
        try:
            ui_max_not = await self.state_hget("state:trade-rules", "max_total_notional_pct")
            if ui_max_not is not None:
                max_notional_total = self.account_value * (float(ui_max_not) / 100.0)
        except Exception:
            pass
        if total_notional >= max_notional_total:
            await self._veto(setup_id, "max_total_notional_exceeded",
                             {"total_notional": round(total_notional, 2),
                              "max_notional": round(max_notional_total, 2)},
                             setup_type=setup_type)
            return

        # ── Gate 4c: market regime check ───────────────────────────────
        # Read the Pulse agent's regime assessment from Redis
        regime = None
        try:
            regime_raw = await self.state_get(SK.MARKET_REGIME)
            if regime_raw:
                regime = json.loads(regime_raw) if isinstance(regime_raw, str) else regime_raw
        except Exception:
            pass

        if regime:
            regime_state = regime.get("regime", "CHOPPY")
            regime_bias = regime.get("bias", "NEUTRAL")

            # RISK_OFF: block ALL new positions
            if regime_state == "RISK_OFF":
                await self._veto(setup_id, "market_regime_risk_off",
                                 {"regime": regime_state, "bias": regime_bias,
                                  "spy_pct": regime.get("spy_change_pct", 0),
                                  "vix": regime.get("vix", 0)},
                                 setup_type=setup_type)
                return

            # BEARISH: block longs unless quality >= 8
            if regime_state == "BEARISH" and direction == "long" and quality < 8:
                await self._veto(setup_id, "bearish_regime_blocks_low_quality_long",
                                 {"regime": regime_state, "quality": quality,
                                  "min_quality_in_bearish": 8},
                                 setup_type=setup_type)
                return

        # ── Gate 5: entry / stop sanity ───────────────────────────────────
        if entry_price <= 0 or stop_price <= 0:
            await self._veto(setup_id, "invalid_price_data",
                             {"entry": entry_price, "stop": stop_price}, setup_type=setup_type)
            return

        # ── Gate 6: position sizing ───────────────────────────────────────
        approved_shares, max_loss_dollars = await self._size_position(entry_price, stop_price)
        if approved_shares <= 0:
            await self._veto(setup_id, "zero_shares_computed",
                             {"entry": entry_price, "stop": stop_price}, setup_type=setup_type)
            return

        # ── Gate 7: single-position heat check ───────────────────────────
        position_heat_pct = (max_loss_dollars / self.account_value) * 100.0
        if position_heat_pct > self.max_position_heat_pct:
            await self._veto(setup_id, "position_heat_exceeded",
                             {"position_heat_pct": round(position_heat_pct, 3),
                              "max_heat_pct": self.max_position_heat_pct}, setup_type=setup_type)
            return

        # ── Gate 8: portfolio heat check ─────────────────────────────────
        current_portfolio_heat = self._current_portfolio_heat_pct(positions)
        projected_heat = current_portfolio_heat + position_heat_pct
        if projected_heat > self.max_portfolio_heat_pct:
            await self._veto(setup_id, "portfolio_heat_exceeded",
                             {"current_heat_pct": round(current_portfolio_heat, 3),
                              "max_heat_pct": self.max_portfolio_heat_pct}, setup_type=setup_type)
            return

        # ── Gate 9: R/R ratio ─────────────────────────────────────────────
        rr = self._calc_rr(entry_price, stop_price, target, direction)
        if rr < self.min_rr_ratio:
            await self._veto(setup_id, "rr_ratio_too_low",
                             {"rr_ratio": rr, "min_required": self.min_rr_ratio}, setup_type=setup_type)
            return

        # ── All gates passed: approve ─────────────────────────────────────
        # Compute a 1–10 heat score for downstream agents (lower is better)
        heat_score = max(1, min(10, int((position_heat_pct / self.max_position_heat_pct) * 10)))

        approved_payload = {
            "setup_id":           setup_id,
            "decision":           "approved",
            "ticker":             ticker,
            "setup_type":         setup_type,
            "direction":          direction,
            "approved_shares":    approved_shares,
            "entry_price":        round(entry_price, 4),
            "stop_price":         round(stop_price, 4),
            "target_price":       round(target, 4),
            "max_loss_dollars":   max_loss_dollars,
            "rr_ratio":           rr,
            "position_heat_score": heat_score,
            "portfolio_heat_pct": round(projected_heat, 3),
        }

        await self.publish(CH.RISK_DECISIONS, approved_payload)

        self.log.info(
            "setup_approved",
            setup_id=setup_id,
            ticker=ticker,
            shares=approved_shares,
            max_loss=max_loss_dollars,
            rr=rr,
            portfolio_heat=round(projected_heat, 3),
        )

    async def _veto(self, setup_id: str, reason: str, extra: dict, setup_type: str = "unknown") -> None:
        """Publish a vetoed decision and log the reason."""
        payload = {
            "setup_id":  setup_id,
            "decision":  "vetoed",
            "setup_type": setup_type,
            "reason":    reason,
        }
        payload.update(extra)
        await self.publish(CH.RISK_DECISIONS, payload)

        self.log.warning("veto", reason=reason, setup_id=setup_id, **extra)

    # ── Fill report handler ───────────────────────────────────────────────────

    async def _handle_fill_report(self, fill: dict) -> None:
        """
        Update in-memory and Redis position state when a fill is confirmed.
        Fills may open or close (or partially close) positions.
        """
        ticker    = fill.get("ticker", "")
        side      = fill.get("side", "")              # "buy" | "sell" | "buy_to_cover" | "sell_short"
        shares    = int(fill.get("filled_qty", 0))
        fill_px   = float(fill.get("fill_price", 0.0))
        stop_px   = float(fill.get("stop_price", 0.0))
        setup_id  = fill.get("setup_id", "")
        direction = fill.get("direction", "long")

        if not ticker or shares <= 0:
            return

        is_open  = side in ("buy", "sell_short")
        is_close = side in ("sell", "buy_to_cover")

        if is_open:
            stop_distance  = abs(fill_px - stop_px) if stop_px > 0 else 0.0
            max_loss       = round(shares * stop_distance, 2) if stop_distance > 0 else 0.0
            position       = {
                "ticker":           ticker,
                "direction":        direction,
                "shares":           shares,
                "entry_price":      fill_px,
                "stop_price":       stop_px,
                "max_loss_dollars": max_loss,
                "setup_id":         setup_id,
                "setup_type":       fill.get("setup_type", "unknown"),
                "opened_at_ms":     await self.now_ms(),
            }
            await self._save_position(ticker, position)
            self.log.info("position_opened",
                          ticker=ticker, shares=shares, entry=fill_px, stop=stop_px)

        elif is_close:
            existing = self._positions.get(ticker)
            pnl = 0.0
            if existing:
                entry      = float(existing.get("entry_price", fill_px))
                pos_dir    = existing.get("direction", "long")
                pos_shares = int(existing.get("shares", shares))
                if pos_dir == "long":
                    pnl = (fill_px - entry) * pos_shares
                else:
                    pnl = (entry - fill_px) * pos_shares
                pnl = round(pnl, 2)

            await self._remove_position(ticker)
            self.log.info("position_closed", ticker=ticker, pnl=pnl)

            # Update daily P&L in state store
            if pnl != 0.0:
                current_pnl_str = await self.state_get(SK.DAILY_PNL) or 0.0
                try:
                    current_pnl = float(current_pnl_str)
                except (TypeError, ValueError):
                    current_pnl = 0.0
                new_pnl = round(current_pnl + pnl, 2)
                await self.state_set(SK.DAILY_PNL, new_pnl)

                # Check drawdown halt after every P&L update
                await self._check_daily_halt(new_pnl)

    # ── Daily halt monitor ────────────────────────────────────────────────────

    async def _check_daily_halt(self, daily_pnl: Optional[float] = None) -> None:
        """
        Trigger a session halt if the daily loss exceeds daily_drawdown_halt_pct.
        The halt is permanent for the session — cannot be reversed here.
        """
        if daily_pnl is None:
            raw = await self.state_get(SK.DAILY_PNL)
            try:
                daily_pnl = float(raw) if raw is not None else 0.0
            except (TypeError, ValueError):
                daily_pnl = 0.0

        if daily_pnl >= 0:
            return  # No loss; nothing to check

        # Check Redis for live UI override of daily loss limit
        halt_pct = self.daily_drawdown_halt_pct
        try:
            ui_halt = await self.state_hget("state:trade-rules", "max_daily_loss_pct")
            if ui_halt is not None:
                halt_pct = float(ui_halt)
        except Exception:
            pass

        loss_pct = abs(daily_pnl) / self.account_value * 100.0
        if loss_pct >= halt_pct:
            # Persist halt flag
            await self.state_hset(SK.RISK_PARAMS, "halted", True)

            halt_payload = {
                "event":              "daily_halt_triggered",
                "daily_pnl":          round(daily_pnl, 2),
                "loss_pct":           round(loss_pct, 3),
                "drawdown_halt_pct":  self.daily_drawdown_halt_pct,
                "account_value":      self.account_value,
                "timestamp_ms":       int(time.time() * 1000),
            }
            await self.publish(CH.DAILY_HALT, halt_payload)

            self.log.warning(
                "daily_halt_triggered",
                daily_pnl=round(daily_pnl, 2),
                loss_pct=round(loss_pct, 3),
                threshold_pct=self.daily_drawdown_halt_pct,
            )

    async def _pnl_monitor_loop(self) -> None:
        """
        Background loop — checks daily P&L against the drawdown threshold every
        30 seconds regardless of fill activity (e.g., for mark-to-market losses).
        """
        while self.running:
            try:
                await self._check_daily_halt()
            except Exception as exc:
                self.log.error("pnl_monitor_error", error=str(exc))
            await asyncio.sleep(30)

    # ── BaseAgent.run() ───────────────────────────────────────────────────────

    async def run(self) -> None:
        self.log.info(
            "actuary_starting",
            account_value=self.account_value,
            max_position_heat_pct=self.max_position_heat_pct,
            max_portfolio_heat_pct=self.max_portfolio_heat_pct,
            daily_drawdown_halt_pct=self.daily_drawdown_halt_pct,
        )

        await self.subscribe(CH.SETUPS, CH.FILL_REPORTS)
        self.log.info("subscribed", channels=[CH.SETUPS, CH.FILL_REPORTS])

        # Run the P&L monitor loop concurrently with the message loop
        asyncio.ensure_future(self._pnl_monitor_loop())

        async for message in self.listen():
            if not self.running:
                break
            try:
                # Route by originating channel — base_agent injects "agent" field;
                # we differentiate by presence of "setup_type" vs "side"
                if "setup_type" in message:
                    await self._evaluate_setup(message)
                elif "side" in message or "filled_qty" in message:
                    await self._handle_fill_report(message)
                else:
                    self.log.debug("unrouted_message", keys=list(message.keys()))
            except Exception as exc:
                self.log.error("message_handling_error", error=str(exc))


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    agent = RiskModelingAgent()
    asyncio.run(agent.start())
