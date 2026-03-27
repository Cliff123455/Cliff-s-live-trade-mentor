# src/coordinator/trade_coordinator.py
# "The General" — synthesizes signals and emits trade directives.
# Only agent authorised to publish to CH.DIRECTIVES.

import asyncio
import json
import time
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

load_dotenv()

from src.shared.base_agent import BaseAgent
from src.shared.constants import CH, SK, AGENTS

# ── Regime constants (must match MarketPulseAgent) ─────────────────────────
REGIME_RISK_OFF = "RISK_OFF"
REGIME_BEARISH  = "BEARISH"
REGIME_BULLISH  = "BULLISH"
REGIME_CHOPPY   = "CHOPPY"


def _load_session_config() -> dict:
    cfg_path = Path(__file__).resolve().parents[2] / "config" / "session.yaml"
    with cfg_path.open() as f:
        return yaml.safe_load(f)


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


class TradeCoordinator(BaseAgent):
    """The General — conviction-gated trade directive emitter."""

    def __init__(self):
        super().__init__(AGENTS.GENERAL)
        cfg = _load_session_config()

        # Session config
        self._session_open: dtime = _parse_hhmm(cfg["session_open"])
        self._session_close: dtime = _parse_hhmm(cfg["session_close"])
        self._prime_windows: list[dict] = [
            {"start": _parse_hhmm(w["start"]), "end": _parse_hhmm(w["end"]), "label": w["label"]}
            for w in cfg.get("prime_windows", [])
        ]
        self._threshold: float = float(cfg["conviction_enter_threshold"])
        self._threshold_off: float = float(cfg["conviction_enter_threshold_off_hours"])
        self._weights: dict = cfg["conviction_weights"]
        self._max_directives: int = int(cfg["max_directives_per_session"])
        self._cooldown_s: float = float(cfg["same_ticker_cooldown_s"])

        # In-memory caches
        self.pending_setups: dict[str, dict] = {}        # setup_id -> setup payload
        self.risk_decisions: dict[str, dict] = {}        # setup_id -> risk decision payload
        self.catalyst_cache: dict[str, dict] = {}        # ticker -> latest catalyst payload
        self.setup_weights: dict[str, float] = {}        # setup_type -> performance weight
        self.directive_count: int = 0
        self.ticker_cooldowns: dict[str, float] = {}     # ticker -> unix timestamp of last directive
        self.emitted_setup_ids: set[str] = set()
        self.halted: bool = False

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _now_et_time(self) -> dtime:
        """Current Eastern time — uses sim clock during backtest, real clock in live."""
        dt = await self.now_et()  # BaseAgent.now_et() respects sim clock
        return dt.time().replace(tzinfo=None)

    def _in_prime_window(self, now: dtime) -> bool:
        for w in self._prime_windows:
            if w["start"] <= now <= w["end"]:
                return True
        return False

    def _in_session(self, now: dtime) -> bool:
        return self._session_open <= now <= self._session_close

    async def _get_threshold(self, now: dtime) -> float:
        """Return conviction threshold — prefer live value from Redis trade-rules,
        fall back to config file value."""
        try:
            live = await self.state_hget("state:trade-rules", "conviction_threshold")
            if live is not None and live != "":
                return float(live)
        except Exception:
            pass
        return self._threshold if self._in_prime_window(now) else self._threshold_off

    async def _get_regime(self) -> dict | None:
        """Read current market regime from The Pulse via Redis state."""
        try:
            raw = await self.state_get(SK.MARKET_REGIME)
            if raw:
                return json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            pass
        return None

    async def _get_markov(self) -> dict | None:
        """Read current Markov prediction from The Oracle via Redis state."""
        try:
            raw = await self.state_get(SK.MARKOV_STATE)
            if raw:
                return json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            pass
        return None

    async def _ticker_in_cooldown(self, ticker: str) -> bool:
        last = self.ticker_cooldowns.get(ticker)
        if last is None:
            return False
        now_s = (await self.now_ms()) / 1000.0
        return (now_s - last) < self._cooldown_s

    def _compute_conviction(self, setup: dict, catalyst: dict | None,
                            now: dtime | None = None, regime: dict | None = None,
                            markov: dict | None = None) -> float:
        """Return conviction score on 0–10 scale, adjusted by market regime and Markov prediction."""
        w = self._weights
        setup_quality_norm = float(setup.get("quality_score", 5.0)) / 10.0
        # Default 0.65 (slightly bullish neutral) when Wire has no catalyst data yet
        catalyst_score = float(catalyst["sentiment_score"]) if catalyst else 0.65
        setup_weight = self.setup_weights.get(setup.get("setup_type", ""), 0.5)
        time_bonus = 1.0 if now and self._in_prime_window(now) else 0.0

        conviction = (
            setup_quality_norm * float(w.get("setup_quality", 0.40))
            + catalyst_score   * float(w.get("catalyst_score", 0.30))
            + setup_weight     * float(w.get("setup_weight", 0.20))
            + time_bonus       * float(w.get("time_of_day_bonus", 0.10))
        ) * 10.0

        # ── Regime adjustment ──────────────────────────────────────────────
        # Penalize conviction when the trade direction fights the market bias
        if regime:
            direction = setup.get("direction", "long")
            regime_state = regime.get("regime", REGIME_CHOPPY)
            bias = regime.get("bias", "NEUTRAL")

            if regime_state == REGIME_BEARISH and direction == "long":
                conviction *= 0.75   # 25% penalty for longs in bearish tape
            elif regime_state == REGIME_BULLISH and direction == "short":
                conviction *= 0.75   # 25% penalty for shorts in bullish tape
            elif regime_state == REGIME_RISK_OFF:
                conviction *= 0.50   # 50% penalty in risk-off (Actuary should block too)

        # ── Markov prediction adjustment ───────────────────────────────────
        # The Oracle provides a conviction_adjustment (±0.20 max) scaled by
        # its own confidence.  Apply it directionally: a bullish Markov
        # prediction boosts longs and penalises shorts, and vice versa.
        if markov and markov.get("observations", 0) >= 25:
            adjustment = float(markov.get("conviction_adjustment", 0.0))
            direction = setup.get("direction", "long")
            markov_bias = markov.get("bias", "NEUTRAL")

            # Flip sign when Markov bias opposes trade direction
            if direction == "long" and markov_bias == "BEARISH":
                adjustment = -abs(adjustment)
            elif direction == "short" and markov_bias == "BULLISH":
                adjustment = -abs(adjustment)
            elif direction == "long" and markov_bias == "BULLISH":
                adjustment = abs(adjustment)
            elif direction == "short" and markov_bias == "BEARISH":
                adjustment = abs(adjustment)

            conviction += adjustment

        return round(conviction, 3)

    @staticmethod
    def _midpoint(zone: list | None, fallback: float) -> float:
        """Return midpoint of a two-element entry_zone list, or fallback."""
        if zone and len(zone) == 2:
            return round((float(zone[0]) + float(zone[1])) / 2.0, 4)
        return float(fallback)

    # ── Message handlers ──────────────────────────────────────────────────────

    async def _handle_setup(self, msg: dict) -> None:
        # Skip diagnostic packets — not real setups
        if msg.get("setup_type") in ("warming_up", "indicator_snapshot"):
            return
        setup_id = msg.get("setup_id")
        if not setup_id:
            self.log.warning("setup_missing_id", msg=msg)
            return
        self.pending_setups[setup_id] = msg
        self.log.debug("setup_cached", setup_id=setup_id, ticker=msg.get("ticker"))

        # If a risk decision already arrived first (race condition), process now
        if setup_id in self.risk_decisions:
            await self._evaluate(setup_id)

    async def _handle_risk_decision(self, msg: dict) -> None:
        setup_id = msg.get("setup_id")
        if not setup_id:
            self.log.warning("risk_decision_missing_id", msg=msg)
            return

        decision = msg.get("decision", "").lower()
        self.risk_decisions[setup_id] = msg

        if decision != "approved":
            self.log.info("risk_rejected", setup_id=setup_id, reason=msg.get("reason"))
            return

        if setup_id in self.pending_setups:
            await self._evaluate(setup_id)
        # else: setup hasn't arrived yet — will be evaluated when setup arrives

    async def _handle_catalyst(self, msg: dict) -> None:
        ticker = msg.get("ticker")
        if ticker:
            self.catalyst_cache[ticker] = msg
            self.log.debug("catalyst_cached", ticker=ticker, score=msg.get("sentiment_score"))

    async def _handle_performance_update(self, msg: dict) -> None:
        setup_type = msg.get("setup_type")
        weight = msg.get("weight")
        if setup_type and weight is not None:
            self.setup_weights[setup_type] = float(weight)
            self.log.debug("weight_updated", setup_type=setup_type, weight=weight)

    async def _handle_regime_update(self, msg: dict) -> None:
        """Cache latest regime for logging — actual regime read happens in _evaluate via Redis."""
        self.log.info("regime_update",
                      regime=msg.get("regime"), bias=msg.get("bias"),
                      spy=msg.get("spy_change_pct"), vix=msg.get("vix"))

    async def _handle_markov_update(self, msg: dict) -> None:
        """Log Markov prediction updates — actual read happens in _evaluate via Redis."""
        self.log.info("markov_update",
                      predicted=msg.get("predicted_next_state"),
                      confidence=msg.get("prediction_confidence"),
                      bias=msg.get("bias"),
                      adjustment=msg.get("conviction_adjustment"))

    async def _handle_daily_halt(self, msg: dict) -> None:
        self.halted = True
        self.log.warning("daily_halt_received", reason=msg.get("reason"), msg=msg)

    # ── Core evaluation ───────────────────────────────────────────────────────

    async def _evaluate(self, setup_id: str) -> None:
        """Called when both setup and an approved risk decision exist for setup_id."""
        if setup_id in self.emitted_setup_ids:
            self.log.debug("duplicate_setup_id_skipped", setup_id=setup_id)
            return

        setup = self.pending_setups[setup_id]
        risk = self.risk_decisions[setup_id]
        ticker = setup.get("ticker", "UNKNOWN")
        now = await self._now_et_time()

        # ── Guard rails ───────────────────────────────────────────────────────
        _stype = setup.get("setup_type", "unknown")

        if self.halted or await self.is_halted():
            self.log.info("pass_halted", setup_id=setup_id, ticker=ticker)
            await self.publish(CH.DIRECTIVES, {"directive_id": self.make_id(), "setup_id": setup_id,
                "ticker": ticker, "action": "pass", "setup_type": _stype, "direction": setup.get("direction",""),
                "rationale": "⛔ Session halted", "conviction_score": 0, "threshold": 0,
                "entry_price": 0, "stop_price": 0, "target_price": 0, "approved_shares": 0})
            return

        if not self._in_session(now):
            self.log.info("pass_outside_session", setup_id=setup_id, ticker=ticker)
            await self.publish(CH.DIRECTIVES, {"directive_id": self.make_id(), "setup_id": setup_id,
                "ticker": ticker, "action": "pass", "setup_type": _stype, "direction": setup.get("direction",""),
                "rationale": f"🕐 Outside session ({now.strftime('%H:%M')} ET)", "conviction_score": 0, "threshold": 0,
                "entry_price": 0, "stop_price": 0, "target_price": 0, "approved_shares": 0})
            return

        if self.directive_count >= self._max_directives:
            self.log.warning("pass_max_directives_reached", setup_id=setup_id, count=self.directive_count)
            await self.publish(CH.DIRECTIVES, {"directive_id": self.make_id(), "setup_id": setup_id,
                "ticker": ticker, "action": "pass", "setup_type": _stype, "direction": setup.get("direction",""),
                "rationale": f"🚫 Max directives ({self._max_directives}) reached", "conviction_score": 0, "threshold": 0,
                "entry_price": 0, "stop_price": 0, "target_price": 0, "approved_shares": 0})
            return

        if await self._ticker_in_cooldown(ticker):
            self.log.info("pass_ticker_cooldown", setup_id=setup_id, ticker=ticker)
            await self.publish(CH.DIRECTIVES, {"directive_id": self.make_id(), "setup_id": setup_id,
                "ticker": ticker, "action": "pass", "setup_type": _stype, "direction": setup.get("direction",""),
                "rationale": f"⏳ {ticker} in cooldown", "conviction_score": 0, "threshold": 0,
                "entry_price": 0, "stop_price": 0, "target_price": 0, "approved_shares": 0})
            return

        # ── Read market regime + Markov prediction ─────────────────────────
        regime = await self._get_regime()
        markov = await self._get_markov()

        # ── Conviction score (regime + Markov adjusted) ──────────────────────
        catalyst = self.catalyst_cache.get(ticker)
        score = self._compute_conviction(setup, catalyst, now, regime, markov)
        threshold = await self._get_threshold(now)

        # Apply regime-based dynamic threshold floor from Pulse
        if regime:
            direction = setup.get("direction", "long")
            if direction == "long":
                regime_floor = regime.get("long_conviction_floor", 0)
            else:
                regime_floor = regime.get("short_conviction_floor", 0)
            # Use the higher of the two thresholds
            if regime_floor > threshold:
                threshold = float(regime_floor)

        self.log.info(
            "conviction_computed",
            setup_id=setup_id,
            ticker=ticker,
            score=score,
            threshold=threshold,
            prime=self._in_prime_window(now),
            regime=regime.get("regime") if regime else "unknown",
            markov_state=markov.get("predicted_next_state") if markov else "n/a",
            markov_adj=markov.get("conviction_adjustment", 0) if markov else 0,
        )

        # ── LLM tie-breaker for borderline cases ──────────────────────────────
        action = None
        rationale = ""

        if 6.5 <= score <= 7.5:
            try:
                action, rationale = await self._llm_decide(setup, risk, catalyst, score, threshold, now, regime)
            except Exception as exc:
                self.log.error("llm_decide_failed", error=str(exc), ticker=ticker)
                action = "pass"
                rationale = f"LLM tie-breaker failed ({exc.__class__.__name__}) — defaulting to pass"
        elif score >= threshold:
            action = "enter"
            rationale = f"Conviction {score:.2f} meets threshold {threshold:.1f}"
        else:
            action = "pass"
            rationale = f"Conviction {score:.2f} below threshold {threshold:.1f}"

        if action == "enter":
            await self._emit_directive(setup, risk, score, rationale)
        else:
            self.log.info(
                "pass",
                setup_id=setup_id,
                ticker=ticker,
                score=score,
                reason=rationale,
            )
            # Publish pass/wait so the General pane shows its reasoning
            await self.publish(CH.DIRECTIVES, {
                "directive_id":   self.make_id(),
                "setup_id":       setup_id,
                "ticker":         ticker,
                "action":         action,          # "pass" or "wait"
                "setup_type":     setup.get("setup_type", "unknown"),
                "direction":      setup.get("direction", "none"),
                "conviction_score": score,
                "threshold":      threshold,
                "rationale":      rationale,
                "entry_price":    0,
                "stop_price":     0,
                "target_price":   0,
                "approved_shares": 0,
            })

    async def _llm_decide(
        self,
        setup: dict,
        risk: dict,
        catalyst: dict | None,
        score: float,
        threshold: float,
        now: dtime,
        regime: dict | None = None,
    ) -> tuple[str, str]:
        """Call LLM for borderline conviction decisions. Returns (action, rationale)."""
        regime_context = ""
        if regime:
            regime_context = (
                f"\nMarket Regime: {regime.get('regime', 'UNKNOWN')} | "
                f"Bias: {regime.get('bias', 'NEUTRAL')} | "
                f"SPY: {regime.get('spy_change_pct', 0):+.1f}% | "
                f"QQQ: {regime.get('qqq_change_pct', 0):+.1f}% | "
                f"VIX: {regime.get('vix', 0)}"
            )

        markov_context = ""
        markov = await self._get_markov()
        if markov and markov.get("observations", 0) >= 25:
            markov_context = (
                f"\nMarkov Prediction: {markov.get('predicted_next_state', 'unknown')} | "
                f"Confidence: {markov.get('prediction_confidence', 0):.0%} | "
                f"Bias: {markov.get('bias', 'NEUTRAL')} | "
                f"Conviction Adj: {markov.get('conviction_adjustment', 0):+.2f}"
            )

        system_prompt = (
            "You are The General, a calm decisive trading coordinator. "
            "You synthesize signals and make the final call. Be conservative. "
            "Consider the overall market regime — do NOT go long in a bearish/risk-off market "
            "unless the setup is exceptional. Return JSON only."
        )
        user_prompt = (
            f"Setup: {json.dumps(setup)}\n"
            f"Risk approval: {json.dumps(risk)}\n"
            f"Catalyst: {json.dumps(catalyst)}\n"
            f"Conviction score: {score}\n"
            f"Time of day: {now.strftime('%H:%M')}\n"
            f"Decision threshold: {threshold}\n"
            f"{regime_context}"
            f"{markov_context}\n\n"
            'Should I enter this trade? Return JSON: {"action": "enter" or "pass", "rationale": "one sentence"}'
        )

        result = await self.llm_json(system_prompt, user_prompt, temperature=0.1)
        action = result.get("action", "pass").lower()
        rationale = result.get("rationale", "LLM borderline decision")

        if action not in ("enter", "pass"):
            self.log.warning("llm_invalid_action", action=action, raw=result)
            action = "pass"
            rationale = "LLM returned invalid action — defaulting to pass"

        self.log.info(
            "llm_decision",
            action=action,
            rationale=rationale,
            score=score,
        )
        return action, rationale

    async def _emit_directive(
        self, setup: dict, risk: dict, conviction_score: float, rationale: str
    ) -> None:
        """Build and publish an ENTER directive to CH.DIRECTIVES."""
        setup_id = setup["setup_id"]
        ticker = setup.get("ticker", "UNKNOWN")

        entry_price = self._midpoint(
            setup.get("entry_zone"), setup.get("entry_price", 0.0)
        )

        directive = {
            "directive_id": self.make_id(),
            "setup_id": setup_id,
            "ticker": ticker,
            "action": "enter",
            "setup_type": setup.get("setup_type", "unknown"),
            "direction": setup.get("direction", risk.get("direction", "long")),
            "entry_price": entry_price,
            "stop_price": float(risk.get("stop_price", setup.get("stop_price", 0.0))),
            "target_price": float(risk.get("target_price", setup.get("target_price", 0.0))),
            "approved_shares": int(risk.get("approved_shares", 0)),
            "time_in_force": setup.get("time_in_force", "day"),
            "conviction_score": conviction_score,
            "rationale": rationale,
        }

        # Mark as emitted before publishing to prevent re-entry on any async interleaving
        self.emitted_setup_ids.add(setup_id)
        self.directive_count += 1
        self.ticker_cooldowns[ticker] = (await self.now_ms()) / 1000.0

        await self.publish(CH.DIRECTIVES, directive)

        self.log.info(
            "directive_emitted",
            directive_id=directive["directive_id"],
            setup_id=setup_id,
            ticker=ticker,
            direction=directive["direction"],
            entry_price=entry_price,
            conviction=conviction_score,
            session_total=self.directive_count,
        )

    # ── Startup: load persisted weights ───────────────────────────────────────

    async def _load_setup_weights(self) -> None:
        try:
            weights = await self.state_hgetall(SK.SETUP_WEIGHTS)
            for k, v in weights.items():
                try:
                    self.setup_weights[k] = float(v)
                except (TypeError, ValueError):
                    pass
            self.log.info("setup_weights_loaded", count=len(self.setup_weights))
        except Exception as exc:
            self.log.warning("setup_weights_load_failed", error=str(exc))

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        await self._load_setup_weights()

        await self.subscribe(
            CH.SETUPS,
            CH.RISK_DECISIONS,
            CH.CATALYSTS,
            CH.PERFORMANCE_UPDATES,
            CH.DAILY_HALT,
            CH.MARKET_REGIME,
            CH.MARKOV_PREDICTION,
        )

        self.log.info(
            "general_ready",
            threshold=self._threshold,
            threshold_off=self._threshold_off,
            max_directives=self._max_directives,
        )

        async for msg in self.listen():
            if not self.running:
                break

            channel = msg.get("channel") or msg.get("_channel", "")

            # Route by channel — base_agent listen() does not attach channel name,
            # so we determine routing by the presence of distinguishing keys.
            msg_type = _classify_message(msg)

            if msg_type == "setup":
                await self._handle_setup(msg)
            elif msg_type == "risk_decision":
                await self._handle_risk_decision(msg)
            elif msg_type == "catalyst":
                await self._handle_catalyst(msg)
            elif msg_type == "performance_update":
                await self._handle_performance_update(msg)
            elif msg_type == "regime":
                await self._handle_regime_update(msg)
            elif msg_type == "markov":
                await self._handle_markov_update(msg)
            elif msg_type == "halt":
                await self._handle_daily_halt(msg)
            else:
                self.log.debug("unrouted_message", keys=list(msg.keys()))


def _classify_message(msg: dict) -> str:
    """Heuristically classify an incoming pub/sub message by its payload keys."""
    if "setup_id" in msg and "quality_score" in msg:
        return "setup"
    if "setup_id" in msg and "decision" in msg:
        return "risk_decision"
    if "sentiment_score" in msg and "catalyst_type" in msg:
        return "catalyst"
    if "win_rate" in msg and "setup_type" in msg:
        return "performance_update"
    # Market regime updates from The Pulse
    if "regime" in msg and "bias" in msg and "long_conviction_floor" in msg:
        return "regime"
    # Markov prediction updates from The Oracle
    if "predicted_next_state" in msg and "conviction_adjustment" in msg:
        return "markov"
    if "halt" in str(msg.get("type", "")).lower() or msg.get("halt"):
        return "halt"
    # Fallback: DAILY_HALT messages often carry a "reason" and agent="actuary"
    if msg.get("agent") and "quality_score" not in msg and "decision" not in msg and "win_rate" not in msg:
        if msg.get("reason") or "daily_loss" in str(msg).lower():
            return "halt"
    return "unknown"


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    agent = TradeCoordinator()
    asyncio.run(agent.start())
