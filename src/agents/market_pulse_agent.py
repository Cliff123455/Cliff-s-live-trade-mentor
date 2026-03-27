# src/agents/market_pulse_agent.py
# "The Pulse" — Market regime detector.
# Reads SPY, QQQ, VIX every N seconds and publishes a regime object
# that other agents consume to make market-aware decisions.
#
# Regime states: BULLISH, BEARISH, CHOPPY, RISK_OFF
# Bias: LONG_PREFERRED, SHORT_PREFERRED, NEUTRAL, NO_NEW_LONGS
#
# Does NOT make LLM calls — pure Python signal processing.

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.shared.base_agent import BaseAgent
from src.shared.constants import AGENTS, CH, SK

load_dotenv()

# ── Regime Constants ──────────────────────────────────────────────────────────

REGIME_BULLISH   = "BULLISH"
REGIME_BEARISH   = "BEARISH"
REGIME_CHOPPY    = "CHOPPY"
REGIME_RISK_OFF  = "RISK_OFF"

BIAS_LONG_PREFERRED  = "LONG_PREFERRED"
BIAS_SHORT_PREFERRED = "SHORT_PREFERRED"
BIAS_NEUTRAL         = "NEUTRAL"
BIAS_NO_NEW_LONGS    = "NO_NEW_LONGS"


class MarketPulseAgent(BaseAgent):
    """The Pulse — reads broad market signals and publishes regime context."""

    def __init__(self, poll_interval_s: float = 60.0):
        super().__init__(AGENTS.PULSE)
        self._poll_interval = poll_interval_s

        # Cached latest prices from the Scanner's market data
        self._spy_prices: list[float] = []
        self._qqq_prices: list[float] = []
        self._vix_level: float = 0.0

        # Session open reference prices (set from first data of the day)
        self._spy_open: Optional[float] = None
        self._qqq_open: Optional[float] = None

        # Current regime
        self._current_regime: dict = self._default_regime()

    @staticmethod
    def _default_regime() -> dict:
        return {
            "regime": REGIME_CHOPPY,
            "bias": BIAS_NEUTRAL,
            "long_conviction_floor": 6,
            "short_conviction_floor": 5,
            "vix_regime": "normal",
            "spy_change_pct": 0.0,
            "qqq_change_pct": 0.0,
            "vix": 0.0,
            "note": "Waiting for market data...",
            "updated_at_ms": 0,
        }

    # ── Regime Computation ─────────────────────────────────────────────────────

    def _compute_regime(self) -> dict:
        """
        Simple rule-based regime detector.
        Uses SPY/QQQ intraday change + VIX level to determine regime and bias.

        Thresholds (tunable):
          - SPY < -1.0%  → BEARISH
          - SPY > +1.0%  → BULLISH
          - VIX > 25     → elevated, VIX > 35 → RISK_OFF
          - SPY between -0.3% and +0.3% → CHOPPY
        """
        spy_pct = 0.0
        qqq_pct = 0.0

        if self._spy_open and self._spy_open > 0 and self._spy_prices:
            spy_now = self._spy_prices[-1]
            spy_pct = ((spy_now - self._spy_open) / self._spy_open) * 100.0

        if self._qqq_open and self._qqq_open > 0 and self._qqq_prices:
            qqq_now = self._qqq_prices[-1]
            qqq_pct = ((qqq_now - self._qqq_open) / self._qqq_open) * 100.0

        vix = self._vix_level

        # ── VIX regime ──────────────────────────────────────────────────────
        if vix >= 35:
            vix_regime = "extreme"
        elif vix >= 25:
            vix_regime = "elevated"
        elif vix >= 18:
            vix_regime = "moderate"
        else:
            vix_regime = "normal"

        # ── Regime classification ───────────────────────────────────────────
        # Priority: RISK_OFF (VIX extreme) > BEARISH > BULLISH > CHOPPY
        if vix >= 35:
            regime = REGIME_RISK_OFF
        elif spy_pct <= -1.5 or qqq_pct <= -2.0:
            regime = REGIME_RISK_OFF
        elif spy_pct <= -0.5 or qqq_pct <= -0.8:
            regime = REGIME_BEARISH
        elif spy_pct >= 1.0 or qqq_pct >= 1.2:
            regime = REGIME_BULLISH
        elif spy_pct >= 0.3:
            regime = REGIME_BULLISH  # mild bullish
        else:
            regime = REGIME_CHOPPY

        # ── Trend direction from recent price action ────────────────────────
        # If we have enough SPY prices, check if trending down intraday
        if len(self._spy_prices) >= 10:
            recent = self._spy_prices[-10:]
            declining = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i-1])
            if declining >= 7 and regime in (REGIME_CHOPPY, REGIME_BEARISH):
                regime = REGIME_BEARISH  # strengthen to bearish on consistent decline

        # ── Bias + conviction floors ────────────────────────────────────────
        if regime == REGIME_RISK_OFF:
            bias = BIAS_NO_NEW_LONGS
            long_floor = 10   # effectively blocks all longs
            short_floor = 4
            note = f"RISK OFF — SPY {spy_pct:+.1f}%, QQQ {qqq_pct:+.1f}%, VIX {vix:.0f}. No new longs."
        elif regime == REGIME_BEARISH:
            bias = BIAS_SHORT_PREFERRED
            long_floor = 8    # only high-conviction longs
            short_floor = 5
            note = f"BEARISH tape — SPY {spy_pct:+.1f}%, QQQ {qqq_pct:+.1f}%. Prefer shorts, longs need 8+ conviction."
        elif regime == REGIME_BULLISH:
            bias = BIAS_LONG_PREFERRED
            long_floor = 5
            short_floor = 7   # only high-conviction shorts
            note = f"BULLISH tape — SPY {spy_pct:+.1f}%, QQQ {qqq_pct:+.1f}%. Prefer longs."
        else:  # CHOPPY
            bias = BIAS_NEUTRAL
            long_floor = 6
            short_floor = 6
            note = f"CHOPPY — SPY {spy_pct:+.1f}%, QQQ {qqq_pct:+.1f}%. Balanced bias."

        # ── Late-day caution ────────────────────────────────────────────────
        # After 3:00 PM ET, raise floors to avoid new positions near close
        # (This is checked by the General, but Pulse can signal it too)

        return {
            "regime": regime,
            "bias": bias,
            "long_conviction_floor": long_floor,
            "short_conviction_floor": short_floor,
            "vix_regime": vix_regime,
            "vix": round(vix, 1),
            "spy_change_pct": round(spy_pct, 2),
            "qqq_change_pct": round(qqq_pct, 2),
            "spy_price": round(self._spy_prices[-1], 2) if self._spy_prices else 0,
            "qqq_price": round(self._qqq_prices[-1], 2) if self._qqq_prices else 0,
            "note": note,
            "updated_at_ms": int(time.time() * 1000),
        }

    # ── Market Data Handler ────────────────────────────────────────────────────

    async def _on_market_data(self, packet: dict) -> None:
        """Ingest market data packets — looking for SPY, QQQ, UVXY/VIX proxies."""
        ticker = str(packet.get("ticker", "")).upper()
        if not ticker or ticker.startswith("_"):
            return

        bid = float(packet.get("bid", 0))
        ask = float(packet.get("ask", 0))
        if bid <= 0 or ask <= 0:
            return
        mid = (bid + ask) / 2.0

        if ticker == "SPY":
            if self._spy_open is None:
                self._spy_open = mid
            self._spy_prices.append(mid)
            # Keep last 200 data points
            if len(self._spy_prices) > 200:
                self._spy_prices = self._spy_prices[-200:]

        elif ticker == "QQQ":
            if self._qqq_open is None:
                self._qqq_open = mid
            self._qqq_prices.append(mid)
            if len(self._qqq_prices) > 200:
                self._qqq_prices = self._qqq_prices[-200:]

        elif ticker in ("UVXY", "VXX", "VIXY"):
            # UVXY is a VIX proxy — rough approximation
            # Real VIX would come from yfinance or another source
            self._vix_level = mid

    # ── Publish Regime ─────────────────────────────────────────────────────────

    async def _publish_regime(self) -> None:
        """Compute and publish the current regime to Redis state + pub/sub."""
        regime = self._compute_regime()
        self._current_regime = regime

        # Write to Redis state so any agent can read it synchronously
        await self.state_set(SK.MARKET_REGIME, json.dumps(regime))

        # Also publish as an event for real-time listeners
        await self.publish(CH.MARKET_REGIME, regime)

        self.log.info(
            "regime_published",
            regime=regime["regime"],
            bias=regime["bias"],
            spy=regime["spy_change_pct"],
            qqq=regime["qqq_change_pct"],
            vix=regime["vix"],
            long_floor=regime["long_conviction_floor"],
        )

    # ── Main Loop ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        # Subscribe to market data to track SPY/QQQ/VIX
        await self.subscribe(CH.MARKET_DATA)
        self.log.info("pulse_ready", poll_interval=self._poll_interval)

        # Background task: publish regime every N seconds
        asyncio.create_task(self._regime_loop())

        # Ingest market data
        async for msg in self.listen():
            if not self.running:
                break
            try:
                await self._on_market_data(msg)
            except Exception as e:
                self.log.error("pulse_data_error", error=str(e))

    async def _regime_loop(self) -> None:
        """Periodically recompute and publish the regime."""
        while self.running:
            await asyncio.sleep(self._poll_interval)
            if not self.running:
                break
            try:
                await self._publish_regime()
            except Exception as exc:
                self.log.error("regime_publish_error", error=str(exc))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    agent = MarketPulseAgent()
    asyncio.run(agent.start())
