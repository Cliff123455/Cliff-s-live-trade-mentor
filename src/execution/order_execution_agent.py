# src/execution/order_execution_agent.py
# "The Sniper" — receives trade directives and executes them via Alpaca.
# Zero hesitation, maximum speed.

import asyncio
import json
import math
import os
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from src.shared.base_agent import BaseAgent
from src.shared.constants import CH, SK, AGENTS

load_dotenv()


def _load_config() -> dict:
    config_path = Path(__file__).resolve().parents[2] / "config" / "execution.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def _is_paper() -> bool:
    return "paper" in os.getenv("ALPACA_BASE_URL", "").lower()


def _tif_enum(tif_str: str) -> TimeInForce:
    mapping = {
        "day": TimeInForce.DAY,
        "gtc": TimeInForce.GTC,
        "ioc": TimeInForce.IOC,
        "fok": TimeInForce.FOK,
        "opg": TimeInForce.OPG,
        "cls": TimeInForce.CLS,
    }
    return mapping.get(tif_str.lower(), TimeInForce.DAY)


class OrderExecutionAgent(BaseAgent):
    """The Sniper — executes trade directives via Alpaca with limit→market fallback."""

    def __init__(self):
        super().__init__(AGENTS.SNIPER)
        self.cfg = _load_config()

        self.alpaca = TradingClient(
            api_key=os.getenv("ALPACA_API_KEY", ""),
            secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
            paper=_is_paper(),
        )

        self.entry_order_type: str = self.cfg.get("entry_order_type", "limit")
        self.limit_fill_timeout_ms: int = self.cfg.get("limit_fill_timeout_ms", 500)
        self.large_order_notional: float = self.cfg.get("large_order_notional", 10000.0)
        self.large_order_slices: int = self.cfg.get("large_order_slices", 3)
        self.large_order_slice_delay_s: float = self.cfg.get("large_order_slice_delay_s", 0.7)
        self.time_in_force: TimeInForce = _tif_enum(self.cfg.get("time_in_force", "day"))

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        await self.subscribe(CH.DIRECTIVES, CH.EXIT_ORDERS)
        self.log.info("sniper_ready", subscriptions=[CH.DIRECTIVES, CH.EXIT_ORDERS])

        async for msg in self.listen():
            if not self.running:
                break
            channel = msg.get("_channel") or msg.get("channel", "")
            # BaseAgent.listen() yields the decoded JSON dict; we need to know
            # which channel it came from. The pubsub message wraps channel info
            # in message["channel"], but listen() strips that. We detect by
            # presence of directive-specific vs exit-specific fields.
            if "action" in msg:
                # Directive message
                await self._handle_directive(msg)
            elif "exit_type" in msg:
                # Exit order from The Watcher
                await self._handle_exit_order(msg)
            else:
                self.log.debug("sniper_unknown_msg", msg=msg)

    # ── Directive handling ────────────────────────────────────────────────────

    async def _handle_directive(self, directive: dict):
        directive_id = directive.get("directive_id")
        action = directive.get("action")

        if not directive_id or action != "enter":
            self.log.debug("sniper_ignored_directive", directive_id=directive_id, action=action)
            return

        if await self.is_halted():
            self.log.warning("sniper_halted_skip", directive_id=directive_id)
            return

        ticker = directive.get("ticker", "")
        direction = directive.get("direction", "long")
        setup_type = directive.get("setup_type", "unknown")
        shares_raw = directive.get("shares") or directive.get("approved_shares") or 0
        shares = int(shares_raw)
        entry_price = float(directive.get("entry_price", 0.0))
        stop_price = float(directive.get("stop_price", 0.0))
        target_price = float(directive.get("target_price", 0.0))

        if not ticker or shares <= 0 or entry_price <= 0:
            self.log.error("sniper_invalid_directive", directive=directive)
            return

        side = OrderSide.BUY if direction == "long" else OrderSide.SELL
        notional = shares * entry_price
        is_large = notional > self.large_order_notional

        self.log.info(
            "sniper_executing",
            directive_id=directive_id,
            ticker=ticker,
            direction=direction,
            shares=shares,
            entry_price=entry_price,
            is_large=is_large,
        )

        if is_large:
            fill = await self._execute_sliced_order(
                directive_id, ticker, side, shares, entry_price
            )
        else:
            fill = await self._execute_single_order(
                directive_id, ticker, side, shares, entry_price
            )

        if fill["status"] in ("filled", "partially_filled") and fill.get("filled_shares", 0) > 0:
            position = {
                "ticker": ticker,
                "directive_id": directive_id,
                "direction": direction,
                "setup_type": setup_type,
                "entry_price": fill["avg_fill_price"],
                "shares": fill["filled_shares"],
                "stop_price": stop_price,
                "target_price": target_price,
                "entry_time_ms": fill["timestamp_ms"],
                "trailing_stop": None,
            }
            await self.state_hset(SK.POSITIONS, ticker, position)

            pending = {
                "directive_id": directive_id,
                "ticker": ticker,
                "order_id": fill["order_id"],
                "side": fill["side"],
                "shares": fill["filled_shares"],
                "entry_price": fill["avg_fill_price"],
                "timestamp_ms": fill["timestamp_ms"],
            }
            await self.state_hset(SK.PENDING_ORDERS, directive_id, pending)

        fill["is_exit"] = False
        fill["setup_type"] = setup_type
        fill["direction"] = direction
        fill["stop_price"] = stop_price
        await self.publish(CH.FILL_REPORTS, fill)

    # ── Exit order handling ───────────────────────────────────────────────────

    async def _handle_exit_order(self, exit_order: dict):
        directive_id = exit_order.get("directive_id", self.make_id())
        ticker = exit_order.get("ticker", "")
        shares = int(exit_order.get("shares", 0))
        exit_type = exit_order.get("exit_type", "unknown")

        if not ticker or shares <= 0:
            self.log.error("sniper_invalid_exit_order", exit_order=exit_order)
            return

        # Determine exit side and setup_type from existing position direction
        position = await self.state_hget(SK.POSITIONS, ticker)
        setup_type_exit = exit_order.get("setup_type", "unknown")
        if position:
            direction = position.get("direction", "long")
            side = OrderSide.SELL if direction == "long" else OrderSide.BUY
            if setup_type_exit == "unknown":
                setup_type_exit = position.get("setup_type", "unknown")
        else:
            # Default: sell to close unknown position
            side = OrderSide.SELL

        self.log.info(
            "sniper_executing_exit",
            directive_id=directive_id,
            ticker=ticker,
            shares=shares,
            exit_type=exit_type,
        )

        fill = await self._execute_market_order(directive_id, ticker, side, shares)
        fill["is_exit"] = True
        fill["exit_type"] = exit_type
        fill["setup_type"] = setup_type_exit

        if fill["status"] == "filled":
            await self.redis.hdel(SK.POSITIONS, ticker)
            await self.redis.hdel(SK.PENDING_ORDERS, directive_id)

        await self.publish(CH.FILL_REPORTS, fill)

    # ── Order execution helpers ───────────────────────────────────────────────

    async def _execute_single_order(
        self,
        directive_id: str,
        ticker: str,
        side: OrderSide,
        shares: int,
        entry_price: float,
    ) -> dict:
        """Place a limit order; fall back to market if not filled in time."""
        try:
            req = LimitOrderRequest(
                symbol=ticker,
                qty=shares,
                side=side,
                time_in_force=self.time_in_force,
                limit_price=round(entry_price, 2),
            )
            order = await asyncio.to_thread(self.alpaca.submit_order, req)
        except Exception as e:
            self.log.error("sniper_limit_submit_failed", ticker=ticker, error=str(e))
            return self._rejected_fill(directive_id, ticker, side, shares, entry_price, reason=f"limit order failed: {e}")

        # Wait for fill
        timeout_s = self.limit_fill_timeout_ms / 1000.0
        filled_order = await self._wait_for_fill(order.id, timeout_s)

        if filled_order and filled_order.status.value == "filled":
            return self._build_fill(directive_id, filled_order, ticker, side, entry_price, is_market=False)

        # Not filled — cancel and place market order
        await self._cancel_order(order.id)
        self.log.info("sniper_limit_timeout_market_fallback", ticker=ticker, order_id=str(order.id))
        return await self._execute_market_order(directive_id, ticker, side, shares, entry_price)

    async def _execute_sliced_order(
        self,
        directive_id: str,
        ticker: str,
        side: OrderSide,
        total_shares: int,
        entry_price: float,
    ) -> dict:
        """Time-slice a large order into N child orders."""
        n = self.large_order_slices
        base_qty = total_shares // n
        remainder = total_shares % n

        total_filled = 0
        total_cost = 0.0
        last_order_id = ""
        last_status = "filled"

        for i in range(n):
            slice_qty = base_qty + (1 if i < remainder else 0)
            if slice_qty == 0:
                continue

            fill = await self._execute_single_order(
                directive_id, ticker, side, slice_qty, entry_price
            )

            if fill["status"] == "filled":
                total_filled += fill["filled_shares"]
                total_cost += fill["filled_shares"] * fill["avg_fill_price"]
                last_order_id = fill["order_id"]
            else:
                last_status = fill["status"]
                self.log.warning(
                    "sniper_slice_failed",
                    directive_id=directive_id,
                    slice=i,
                    status=fill["status"],
                )

            if i < n - 1:
                await asyncio.sleep(self.large_order_slice_delay_s)

        if total_filled == 0:
            return self._rejected_fill(directive_id, ticker, side, total_shares, entry_price, reason="all slices failed")

        avg_price = total_cost / total_filled
        slippage = round((avg_price - entry_price) * 100) if side == OrderSide.BUY else round((entry_price - avg_price) * 100)

        return {
            "directive_id": directive_id,
            "order_id": last_order_id,
            "ticker": ticker,
            "status": last_status if total_filled < total_shares else "filled",
            "side": side.value.lower(),
            "filled_shares": total_filled,
            "avg_fill_price": round(avg_price, 4),
            "fill_price": round(avg_price, 4),
            "shares": total_filled,
            "slippage_cents": slippage,
            "timestamp_ms": int(time.time() * 1000),
        }

    async def _execute_market_order(
        self,
        directive_id: str,
        ticker: str,
        side: OrderSide,
        shares: int,
        entry_price: float = 0.0,
    ) -> dict:
        """Place a market order directly."""
        try:
            req = MarketOrderRequest(
                symbol=ticker,
                qty=shares,
                side=side,
                time_in_force=self.time_in_force,
            )
            order = await asyncio.to_thread(self.alpaca.submit_order, req)
        except Exception as e:
            self.log.error("sniper_market_submit_failed", ticker=ticker, error=str(e))
            return self._rejected_fill(directive_id, ticker, side, shares, entry_price, reason=f"market order failed: {e}")

        # Market orders fill very quickly; give a generous wait
        filled_order = await self._wait_for_fill(order.id, timeout_s=5.0)

        if filled_order and filled_order.status.value == "filled":
            return self._build_fill(directive_id, filled_order, ticker, side, entry_price, is_market=True)

        # Return partial / pending fill
        avg_price = float(filled_order.filled_avg_price or entry_price) if filled_order else entry_price
        filled_qty = int(float(filled_order.filled_qty or 0)) if filled_order else 0
        status = filled_order.status.value if filled_order else "pending"

        self.log.warning("sniper_market_not_filled", ticker=ticker, order_id=str(order.id), status=status)
        return {
            "directive_id": directive_id,
            "order_id": str(order.id),
            "ticker": ticker,
            "status": status,
            "side": side.value.lower(),
            "filled_shares": filled_qty,
            "avg_fill_price": round(avg_price, 4),
            "fill_price": round(avg_price, 4),
            "shares": filled_qty,
            "slippage_cents": 0,
            "timestamp_ms": int(time.time() * 1000),
        }

    # ── Alpaca helpers ────────────────────────────────────────────────────────

    async def _wait_for_fill(self, order_id, timeout_s: float):
        """Poll Alpaca until filled, cancelled, or timeout."""
        deadline = time.monotonic() + timeout_s
        poll_interval = 0.05  # 50 ms

        while time.monotonic() < deadline:
            try:
                order = await asyncio.to_thread(self.alpaca.get_order_by_id, str(order_id))
                status = order.status.value
                if status in ("filled", "partially_filled", "canceled", "expired", "rejected"):
                    return order
            except Exception as e:
                self.log.warning("sniper_poll_error", order_id=str(order_id), error=str(e))
            await asyncio.sleep(poll_interval)

        # Final check
        try:
            return await asyncio.to_thread(self.alpaca.get_order_by_id, str(order_id))
        except Exception:
            return None

    async def _cancel_order(self, order_id):
        try:
            await asyncio.to_thread(self.alpaca.cancel_order_by_id, str(order_id))
            self.log.info("sniper_order_cancelled", order_id=str(order_id))
        except Exception as e:
            self.log.warning("sniper_cancel_failed", order_id=str(order_id), error=str(e))

    # ── Fill builders ─────────────────────────────────────────────────────────

    def _build_fill(
        self,
        directive_id: str,
        order,
        ticker: str,
        side: OrderSide,
        intended_price: float,
        is_market: bool,
    ) -> dict:
        avg_price = float(order.filled_avg_price or intended_price)
        filled_qty = int(float(order.filled_qty or 0))
        slippage = round((avg_price - intended_price) * 100) if side == OrderSide.BUY else round((intended_price - avg_price) * 100)

        return {
            "directive_id": directive_id,
            "order_id": str(order.id),
            "ticker": ticker,
            "status": "filled",
            "side": side.value.lower(),
            "filled_shares": filled_qty,
            "avg_fill_price": round(avg_price, 4),
            "fill_price": round(avg_price, 4),
            "shares": filled_qty,
            "slippage_cents": slippage,
            "timestamp_ms": int(time.time() * 1000),
        }

    def _rejected_fill(
        self,
        directive_id: str,
        ticker: str,
        side: OrderSide,
        shares: int,
        entry_price: float,
        reason: str = "unknown",
    ) -> dict:
        return {
            "directive_id": directive_id,
            "order_id": "",
            "ticker": ticker,
            "status": "rejected",
            "reason": reason,
            "side": side.value.lower(),
            "filled_shares": 0,
            "avg_fill_price": 0.0,
            "fill_price": 0.0,
            "shares": 0,
            "slippage_cents": 0,
            "timestamp_ms": int(time.time() * 1000),
        }


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    agent = OrderExecutionAgent()
    try:
        await agent.start()
    except KeyboardInterrupt:
        await agent.stop()


if __name__ == "__main__":
    asyncio.run(main())
