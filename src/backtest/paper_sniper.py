# src/backtest/paper_sniper.py
# "The Dummy Sniper" — simulates order fills without touching Alpaca.
# Fills at the directive's entry price with zero slippage (or configurable slippage).
# Publishes fill reports in the exact same format as the real Sniper.

import asyncio
import json
import time
import uuid
from pathlib import Path

import yaml

from src.shared.base_agent import BaseAgent
from src.shared.constants import CH, SK, AGENTS


def _load_config() -> dict:
    config_path = Path(__file__).resolve().parents[2] / "config" / "execution.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


class PaperSniper(BaseAgent):
    """Simulates the Sniper — instant fills at directive price, no Alpaca calls."""

    def __init__(self, slippage_bps: float = 0.0):
        """
        Args:
            slippage_bps: Simulated slippage in basis points (e.g. 5.0 = 0.05%).
                          Applied against the trade direction (costs money).
        """
        super().__init__(AGENTS.SNIPER)
        self.cfg = _load_config()
        self.slippage_bps = slippage_bps

    async def run(self):
        await self.subscribe(CH.DIRECTIVES, CH.EXIT_ORDERS)
        self.log.info("paper_sniper_started", slippage_bps=self.slippage_bps)

        async for msg in self.listen():
            channel = msg.get("_channel", "")

            if "exit" in channel or msg.get("exit_type"):
                await self._handle_exit(msg)
            elif msg.get("action") == "enter":
                await self._handle_entry(msg)

    async def _handle_entry(self, directive: dict):
        """Simulate an entry fill."""
        ticker = directive.get("ticker", "???")
        direction = directive.get("direction", "long")
        setup_type = directive.get("setup_type", "unknown")
        shares = int(directive.get("approved_shares", directive.get("shares", 0)))
        entry_price = float(directive.get("entry_price", 0))
        stop_price = float(directive.get("stop_price", 0))
        target_price = float(directive.get("target_price", 0))
        directive_id = directive.get("directive_id", str(uuid.uuid4()))

        if shares <= 0 or entry_price <= 0:
            self.log.warning("paper_sniper_bad_directive", directive_id=directive_id, shares=shares, price=entry_price)
            return

        # Apply slippage
        slip_pct = self.slippage_bps / 10000.0
        if direction == "long":
            fill_price = round(entry_price * (1 + slip_pct), 4)
        else:
            fill_price = round(entry_price * (1 - slip_pct), 4)

        slippage_cents = round((fill_price - entry_price) * 100)
        side = "buy" if direction == "long" else "sell"

        fill = {
            "directive_id": directive_id,
            "order_id": f"paper-{uuid.uuid4().hex[:12]}",
            "ticker": ticker,
            "status": "filled",
            "side": side,
            "direction": direction,
            "setup_type": setup_type,
            "filled_shares": shares,
            "avg_fill_price": fill_price,
            "fill_price": fill_price,
            "shares": shares,
            "stop_price": stop_price,
            "slippage_cents": slippage_cents,
            "timestamp_ms": await self.now_ms(),
            "is_exit": False,
            "_paper": True,
        }

        self.log.info(
            "paper_sniper_filled",
            ticker=ticker,
            direction=direction,
            shares=shares,
            price=fill_price,
            slip=f"{slippage_cents}c",
        )

        # Write position to Redis (same as real Sniper)
        entry_ts = fill["timestamp_ms"]
        position = {
            "ticker": ticker,
            "directive_id": directive_id,
            "direction": direction,
            "setup_type": setup_type,
            "entry_price": fill_price,
            "shares": shares,
            "stop_price": stop_price,
            "target_price": target_price,
            "entry_time_ms": entry_ts,
            "trailing_stop": None,
        }
        await self.state_hset(SK.POSITIONS, ticker, position)

        pending = {
            "directive_id": directive_id,
            "ticker": ticker,
            "order_id": fill["order_id"],
            "side": side,
            "shares": shares,
            "entry_price": fill_price,
            "timestamp_ms": fill["timestamp_ms"],
        }
        await self.state_hset(SK.PENDING_ORDERS, directive_id, pending)

        await self.publish(CH.FILL_REPORTS, fill)

    async def _handle_exit(self, exit_order: dict):
        """Simulate an exit fill."""
        ticker = exit_order.get("ticker", "???")
        shares = int(exit_order.get("shares", 0))
        exit_price = float(exit_order.get("exit_price", exit_order.get("current_price", 0)))
        exit_type = exit_order.get("exit_type", "unknown")
        directive_id = exit_order.get("directive_id", str(uuid.uuid4()))
        direction = exit_order.get("direction", "long")
        setup_type = exit_order.get("setup_type", "unknown")

        if shares <= 0:
            return

        side = "sell" if direction == "long" else "buy"

        fill = {
            "directive_id": directive_id,
            "order_id": f"paper-exit-{uuid.uuid4().hex[:12]}",
            "ticker": ticker,
            "status": "filled",
            "side": side,
            "direction": direction,
            "setup_type": setup_type,
            "filled_shares": shares,
            "avg_fill_price": exit_price,
            "fill_price": exit_price,
            "shares": shares,
            "exit_price_approx": exit_price,
            "slippage_cents": 0,
            "timestamp_ms": await self.now_ms(),
            "is_exit": True,
            "exit_type": exit_type,
            "_paper": True,
        }

        self.log.info(
            "paper_sniper_exit",
            ticker=ticker,
            exit_type=exit_type,
            shares=shares,
            price=exit_price,
        )

        # Remove position from Redis
        await self.redis.hdel(SK.POSITIONS, ticker)
        await self.redis.hdel(SK.PENDING_ORDERS, directive_id)

        await self.publish(CH.FILL_REPORTS, fill)
