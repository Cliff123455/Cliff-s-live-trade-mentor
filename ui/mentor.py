# ui/mentor.py
# AI Mentor chat backend — provides /api/mentor/* endpoints.
# The Mentor observes all Redis state and answers questions about
# agent decisions, trade setups, risk vetoes, and market conditions.
# It NEVER executes trades — read-only observer + teacher.

import json
import os
import time

import redis
from flask import Blueprint, jsonify, request
from openai import OpenAI

from src.shared.constants import SK, AGENTS, DEFAULT_MODELS, CH

mentor_bp = Blueprint("mentor", __name__)

# ── Redis helper (reuses pattern from app.py) ────────────────────────────────
_redis: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True,
        )
    return _redis


# ── OpenRouter client ────────────────────────────────────────────────────────
_llm: OpenAI | None = None


def _get_llm() -> OpenAI:
    global _llm
    if _llm is None:
        _llm = OpenAI(
            api_key=os.getenv("OPENROUTER_API_KEY", ""),
            base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        )
    return _llm


# ── Conversation history (in-memory, per session) ───────────────────────────
# Keeps last N messages for context. Resets on server restart.
_chat_history: list[dict] = []
MAX_HISTORY = 20


# ── Gather full Redis context for the system prompt ─────────────────────────
def _gather_context() -> str:
    """Read all relevant Redis state and format it as context for the mentor."""
    r = _get_redis()
    sections = []

    # 1. Daily P&L
    pnl_raw = r.get(SK.DAILY_PNL)
    daily_pnl = float(pnl_raw) if pnl_raw else 0.0
    sections.append(f"## Daily P&L\n${daily_pnl:+.2f}")

    # 2. Trade rules (live overrides)
    rules = r.hgetall("state:trade-rules")
    if rules:
        rules_str = "\n".join(f"  {k}: {v}" for k, v in rules.items())
        sections.append(f"## Active Trade Rules\n{rules_str}")

    # 3. Risk parameters & halt status
    risk = r.hgetall(SK.RISK_PARAMS)
    if risk:
        halted = risk.get("halted", "false")
        risk_str = "\n".join(f"  {k}: {v}" for k, v in risk.items())
        sections.append(f"## Risk State (halted={halted})\n{risk_str}")

    # 4. Open positions
    raw_positions = r.hgetall(SK.POSITIONS)
    if raw_positions:
        pos_lines = []
        for ticker, val in raw_positions.items():
            try:
                p = json.loads(val)
                direction = p.get("direction", "long")
                entry = p.get("entry_price", 0)
                shares = p.get("shares", 0)
                stop = p.get("stop_price", 0)
                target = p.get("target_price", 0)
                trailing = p.get("trailing_stop", "—")
                pos_lines.append(
                    f"  {ticker}: {direction.upper()} {shares}sh @${entry:.2f} "
                    f"stop=${stop:.2f} target=${target:.2f} trail={trailing}"
                )
            except (json.JSONDecodeError, TypeError):
                pos_lines.append(f"  {ticker}: {val}")
        sections.append(f"## Open Positions\n" + "\n".join(pos_lines))
    else:
        sections.append("## Open Positions\nNone")

    # 5. Agent statuses
    agent_lines = []
    for name in AGENTS.ALL:
        raw = r.hgetall(SK.AGENT_STATUS.format(name))
        status = raw.get("status", "stopped")
        ts = int(raw.get("ts", 0))
        age_s = (time.time() * 1000 - ts) / 1000 if ts else 999
        agent_lines.append(f"  {name}: {status} (last heartbeat {age_s:.0f}s ago)")
    sections.append("## Agent Statuses\n" + "\n".join(agent_lines))

    # 6. Current models per agent
    models = {}
    for name in AGENTS.ALL:
        val = r.hget(SK.MODELS, name)
        models[name] = val or DEFAULT_MODELS.get(name, "unknown")
    model_lines = [f"  {k}: {v}" for k, v in models.items()]
    sections.append("## Agent Models\n" + "\n".join(model_lines))

    # 7. Active setups (last few from the list)
    try:
        setups_raw = r.lrange(SK.SETUPS, -5, -1)
        if setups_raw:
            setup_lines = []
            for s in setups_raw:
                try:
                    setup = json.loads(s)
                    ticker = setup.get("ticker", "?")
                    stype = setup.get("setup_type", "?")
                    quality = setup.get("quality_score", "?")
                    direction = setup.get("direction", "?")
                    setup_lines.append(f"  {ticker}: {stype} {direction} Q={quality}")
                except (json.JSONDecodeError, TypeError):
                    pass
            if setup_lines:
                sections.append("## Recent Setups\n" + "\n".join(setup_lines))
    except Exception:
        pass

    # 8. Setup performance weights (from Historian)
    weights = r.hgetall(SK.SETUP_WEIGHTS)
    if weights:
        w_lines = [f"  {k}: {v}" for k, v in weights.items()]
        sections.append("## Setup Win-Rate Weights\n" + "\n".join(w_lines))

    # 9. Sim clock (if in backtest mode)
    sim_clock = r.get("state:sim-clock")
    if sim_clock:
        from datetime import datetime
        try:
            sim_dt = datetime.fromtimestamp(int(sim_clock) / 1000)
            sections.append(f"## Mode: BACKTEST\nSimulated time: {sim_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception:
            sections.append(f"## Mode: BACKTEST\nSim clock: {sim_clock}")
    else:
        sections.append("## Mode: LIVE")

    return "\n\n".join(sections)


MENTOR_SYSTEM_PROMPT = """You are the CliffClaw AI Mentor — a senior day trader sitting next to Cliff, watching the same screens, answering questions in plain English.

You have FULL READ ACCESS to the live trading system state (provided below). You can see:
- All agent statuses (Scanner, Wire, Chartist, Actuary, General, Sniper, Watcher, Historian)
- Open positions, P&L, risk state
- Trade rules and conviction thresholds
- Recent setups and their quality scores
- Which LLM models each agent is using

Your job:
1. EXPLAIN what the agents are doing and why
2. TEACH trading concepts (setups, patterns, risk management) in plain language
3. ANALYZE current conditions using the live data
4. ANSWER questions about specific trades, decisions, and vetoes

You are NOT allowed to:
- Place trades or modify any system state
- Give financial advice (always caveat with "this is educational, not advice")
- Make up data — only reference what's in the context below

Keep answers concise but thorough. Use specific numbers from the data when available.
If you don't have enough data to answer (e.g., no positions, agents stopped), say so.

--- LIVE SYSTEM STATE ---
{context}
--- END STATE ---"""


# ── Routes ───────────────────────────────────────────────────────────────────

@mentor_bp.route("/api/mentor/model", methods=["GET"])
def mentor_model_get():
    """Return current mentor model ID."""
    try:
        r = _get_redis()
        model = r.get("mentor:model")
        default = os.getenv("ANALYSIS_MODEL", "anthropic/claude-3.5-sonnet")
        return jsonify({"model": model or default})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@mentor_bp.route("/api/mentor/model", methods=["POST"])
def mentor_model_post():
    """Save a new mentor model ID."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        model = data.get("model", "").strip()
        if not model:
            return jsonify({"error": "model cannot be empty"}), 400
        r = _get_redis()
        r.set("mentor:model", model)
        return jsonify({"ok": True, "model": model})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@mentor_bp.route("/api/mentor/chat", methods=["POST"])
def mentor_chat():
    """Send a message to the mentor, get a response with full system context."""
    global _chat_history
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_message = data.get("message", "").strip()
        if not user_message:
            return jsonify({"error": "message cannot be empty"}), 400

        # Get model
        r = _get_redis()
        model = r.get("mentor:model")
        if not model:
            model = os.getenv("ANALYSIS_MODEL", "anthropic/claude-3.5-sonnet")

        # Gather live context
        context = _gather_context()
        system_prompt = MENTOR_SYSTEM_PROMPT.format(context=context)

        # Build messages with history
        messages = [{"role": "system", "content": system_prompt}]

        # Add conversation history (last N exchanges)
        for msg in _chat_history[-MAX_HISTORY:]:
            messages.append(msg)

        # Add current user message
        messages.append({"role": "user", "content": user_message})

        # Call LLM
        llm = _get_llm()
        response = llm.chat.completions.create(
            model=model,
            temperature=0.3,
            messages=messages,
        )
        reply = response.choices[0].message.content or ""

        # Update history
        _chat_history.append({"role": "user", "content": user_message})
        _chat_history.append({"role": "assistant", "content": reply})

        # Trim history
        if len(_chat_history) > MAX_HISTORY * 2:
            _chat_history = _chat_history[-MAX_HISTORY * 2:]

        return jsonify({
            "reply": reply,
            "model": model,
            "context_size": len(context),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@mentor_bp.route("/api/mentor/clear", methods=["POST"])
def mentor_clear_history():
    """Clear conversation history."""
    global _chat_history
    _chat_history = []
    return jsonify({"ok": True})
