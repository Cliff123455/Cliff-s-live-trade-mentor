# src/agents/markov_regime_agent.py
# "The Oracle" — Markov Chain regime predictor for live trading.
#
# Observes real-time price returns from the Scanner's market data stream,
# classifies the current market micro-state, builds a transition probability
# matrix from observed state sequences, and predicts the most likely next
# state.  Publishes predictions to CH.MARKOV_PREDICTION for The General to
# incorporate into conviction scoring.
#
# States are discretised from two features computed on a rolling window:
#   1. Return (price change %)  → { strong_down, down, flat, up, strong_up }
#   2. Volatility (std of returns) → { low_vol, high_vol }
#
# This gives 10 composite states (e.g. "up|low_vol", "strong_down|high_vol").
# The transition matrix is updated online with every new observation and uses
# exponential decay so recent transitions matter more than stale ones.
#
# Does NOT make LLM calls — pure Python signal processing.

import asyncio
import json
import math
import time
from collections import defaultdict
from typing import Optional

from src.shared.base_agent import BaseAgent
from src.shared.constants import AGENTS, CH, SK

# ── Markov States ────────────────────────────────────────────────────────────

# Return buckets (percentage thresholds on rolling window returns)
RETURN_STRONG_DOWN = "strong_down"
RETURN_DOWN        = "down"
RETURN_FLAT        = "flat"
RETURN_UP          = "up"
RETURN_STRONG_UP   = "strong_up"

# Volatility buckets
VOL_LOW  = "low_vol"
VOL_HIGH = "high_vol"

# Thresholds (tunable)
RETURN_STRONG_THRESH = 0.30   # ±0.30% for strong move on 1-min returns
RETURN_MILD_THRESH   = 0.08   # ±0.08% for mild move
VOL_THRESH           = 0.15   # Std-dev of returns above this → high vol

# All possible composite states
RETURN_BUCKETS = [RETURN_STRONG_DOWN, RETURN_DOWN, RETURN_FLAT, RETURN_UP, RETURN_STRONG_UP]
VOL_BUCKETS    = [VOL_LOW, VOL_HIGH]
ALL_STATES     = [f"{r}|{v}" for r in RETURN_BUCKETS for v in VOL_BUCKETS]

# Directional bias mapping — what each predicted state means for trading
STATE_BIAS = {
    "strong_up|low_vol":    ("BULLISH",  0.15),   # (bias, conviction_adjustment)
    "strong_up|high_vol":   ("BULLISH",  0.10),   # bullish but volatile = less confident
    "up|low_vol":           ("BULLISH",  0.10),
    "up|high_vol":          ("NEUTRAL",  0.00),   # up but shaky
    "flat|low_vol":         ("NEUTRAL",  0.00),
    "flat|high_vol":        ("CHOPPY",  -0.05),   # flat + volatile = choppy
    "down|low_vol":         ("BEARISH", -0.10),
    "down|high_vol":        ("BEARISH", -0.15),
    "strong_down|low_vol":  ("BEARISH", -0.15),
    "strong_down|high_vol": ("BEARISH", -0.20),   # strong sell-off in chaos
}


class MarkovRegimeAgent(BaseAgent):
    """The Oracle — Markov chain market state predictor for live trading."""

    def __init__(self, publish_interval_s: float = 30.0, window_size: int = 20,
                 decay_factor: float = 0.95):
        super().__init__(AGENTS.ORACLE)
        self._publish_interval = publish_interval_s
        self._window_size = window_size       # Rolling window for return/vol calc
        self._decay = decay_factor            # Exponential decay on transition counts

        # Per-ticker price histories (mid prices)
        self._prices: dict[str, list[float]] = defaultdict(list)

        # Transition matrix: counts[from_state][to_state] = weighted count
        self._transitions: dict[str, dict[str, float]] = {
            s: {t: 0.0 for t in ALL_STATES} for s in ALL_STATES
        }

        # State sequence for SPY (primary signal)
        self._state_history: list[str] = []
        self._current_state: Optional[str] = None

        # Latest prediction (cached for Redis state)
        self._prediction: dict = self._default_prediction()

        # Track how many observations we've ingested
        self._observation_count: int = 0

    @staticmethod
    def _default_prediction() -> dict:
        return {
            "current_state": "flat|low_vol",
            "predicted_next_state": "flat|low_vol",
            "prediction_confidence": 0.0,
            "transition_probabilities": {},
            "bias": "NEUTRAL",
            "conviction_adjustment": 0.0,
            "observations": 0,
            "note": "Warming up — collecting price data...",
            "updated_at_ms": 0,
        }

    # ── State Classification ─────────────────────────────────────────────────

    @staticmethod
    def _classify_return(pct_return: float) -> str:
        """Classify a percentage return into a discrete bucket."""
        if pct_return <= -RETURN_STRONG_THRESH:
            return RETURN_STRONG_DOWN
        elif pct_return <= -RETURN_MILD_THRESH:
            return RETURN_DOWN
        elif pct_return >= RETURN_STRONG_THRESH:
            return RETURN_STRONG_UP
        elif pct_return >= RETURN_MILD_THRESH:
            return RETURN_UP
        else:
            return RETURN_FLAT

    @staticmethod
    def _classify_volatility(returns: list[float]) -> str:
        """Classify volatility from a list of returns."""
        if len(returns) < 2:
            return VOL_LOW
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        std = math.sqrt(variance)
        return VOL_HIGH if std > VOL_THRESH else VOL_LOW

    def _get_composite_state(self, ticker: str) -> Optional[str]:
        """Compute the current composite state for a ticker from its price history."""
        prices = self._prices.get(ticker, [])
        if len(prices) < self._window_size + 1:
            return None

        # Compute returns over the rolling window
        window = prices[-(self._window_size + 1):]
        returns = []
        for i in range(1, len(window)):
            if window[i - 1] > 0:
                ret = ((window[i] - window[i - 1]) / window[i - 1]) * 100.0
                returns.append(ret)

        if not returns:
            return None

        # Current return = latest return in the window
        current_return = returns[-1]
        return_bucket = self._classify_return(current_return)
        vol_bucket = self._classify_volatility(returns)

        return f"{return_bucket}|{vol_bucket}"

    # ── Transition Matrix ────────────────────────────────────────────────────

    def _record_transition(self, from_state: str, to_state: str) -> None:
        """Record a state transition with exponential decay on older observations."""
        # Decay all existing counts
        for s in ALL_STATES:
            for t in ALL_STATES:
                self._transitions[s][t] *= self._decay

        # Increment the observed transition
        self._transitions[from_state][to_state] += 1.0

    def _get_transition_probs(self, from_state: str) -> dict[str, float]:
        """Get normalised transition probabilities from a given state."""
        row = self._transitions[from_state]
        total = sum(row.values())
        if total <= 0:
            # Uniform prior if no data
            n = len(ALL_STATES)
            return {s: 1.0 / n for s in ALL_STATES}
        return {s: round(count / total, 4) for s, count in row.items()}

    def _predict_next_state(self) -> tuple[str, float, dict[str, float]]:
        """Predict the most likely next state given the current state.
        Returns (predicted_state, confidence, full_probabilities)."""
        if not self._current_state:
            return "flat|low_vol", 0.0, {}

        probs = self._get_transition_probs(self._current_state)
        best_state = max(probs, key=probs.get)
        confidence = probs[best_state]

        # Only return non-trivial probabilities (> 1%)
        significant = {s: p for s, p in probs.items() if p > 0.01}

        return best_state, confidence, significant

    # ── Market Data Handler ──────────────────────────────────────────────────

    async def _on_market_data(self, packet: dict) -> None:
        """Ingest market data — build price history for SPY (primary signal)."""
        ticker = str(packet.get("ticker", "")).upper()
        if not ticker or ticker.startswith("_"):
            return

        bid = float(packet.get("bid", 0))
        ask = float(packet.get("ask", 0))
        if bid <= 0 or ask <= 0:
            return
        mid = (bid + ask) / 2.0

        self._prices[ticker].append(mid)

        # Cap history at 500 points per ticker
        if len(self._prices[ticker]) > 500:
            self._prices[ticker] = self._prices[ticker][-500:]

        # Only build the Markov chain from SPY (broad market proxy)
        if ticker != "SPY":
            return

        new_state = self._get_composite_state("SPY")
        if new_state is None:
            return

        self._observation_count += 1

        # Record transition if we have a previous state
        if self._current_state and new_state != self._current_state:
            self._record_transition(self._current_state, new_state)

        self._current_state = new_state
        self._state_history.append(new_state)

        # Keep last 1000 state observations
        if len(self._state_history) > 1000:
            self._state_history = self._state_history[-1000:]

    # ── Publish Prediction ───────────────────────────────────────────────────

    async def _publish_prediction(self) -> None:
        """Compute and publish the Markov prediction to Redis + pub/sub."""
        predicted_state, confidence, probs = self._predict_next_state()

        bias_info = STATE_BIAS.get(predicted_state, ("NEUTRAL", 0.0))

        # Scale conviction adjustment by confidence (low confidence = less impact)
        raw_adjustment = bias_info[1]
        scaled_adjustment = round(raw_adjustment * confidence, 4)

        min_observations = self._window_size + 5  # Need enough data to be meaningful
        if self._observation_count < min_observations:
            note = (f"Warming up — {self._observation_count}/{min_observations} "
                    f"observations collected")
            scaled_adjustment = 0.0
            confidence = 0.0
        else:
            state_label = predicted_state.replace("|", ", ")
            note = (f"Predicting {state_label} next "
                    f"(confidence {confidence:.0%}). "
                    f"Conviction adjustment: {scaled_adjustment:+.2f}")

        prediction = {
            "current_state": self._current_state or "unknown",
            "predicted_next_state": predicted_state,
            "prediction_confidence": round(confidence, 4),
            "transition_probabilities": probs,
            "bias": bias_info[0],
            "conviction_adjustment": scaled_adjustment,
            "observations": self._observation_count,
            "state_history_length": len(self._state_history),
            "note": note,
            "updated_at_ms": int(time.time() * 1000),
        }

        self._prediction = prediction

        # Write to Redis state for synchronous reads
        await self.state_set(SK.MARKOV_STATE, json.dumps(prediction))

        # Publish as event for real-time listeners
        await self.publish(CH.MARKOV_PREDICTION, prediction)

        self.log.info(
            "markov_prediction",
            current=self._current_state,
            predicted=predicted_state,
            confidence=round(confidence, 3),
            bias=bias_info[0],
            adjustment=scaled_adjustment,
            observations=self._observation_count,
        )

    # ── Main Loop ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        await self.subscribe(CH.MARKET_DATA)
        self.log.info("oracle_ready", publish_interval=self._publish_interval,
                      window_size=self._window_size, decay=self._decay)

        # Background task: publish predictions periodically
        asyncio.create_task(self._prediction_loop())

        # Ingest market data
        async for msg in self.listen():
            if not self.running:
                break
            try:
                await self._on_market_data(msg)
            except Exception as e:
                self.log.error("oracle_data_error", error=str(e))

    async def _prediction_loop(self) -> None:
        """Periodically compute and publish Markov predictions."""
        while self.running:
            await asyncio.sleep(self._publish_interval)
            if not self.running:
                break
            try:
                await self._publish_prediction()
            except Exception as exc:
                self.log.error("prediction_publish_error", error=str(exc))


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    agent = MarkovRegimeAgent()
    asyncio.run(agent.start())
