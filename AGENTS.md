# AGENTS.md — ScalpBot Agent Communication Contracts

> **Read this second, after `.antigravity/rules.md`.**
> This is the single source of truth for every agent's role, message channels,
> payload schemas, and state store keys. Update this file whenever you change
> a channel name, payload field, or state key — before you merge.

---

## Architecture Overview

ScalpBot is a five-tier multi-agent system. Each tier has a dedicated job.
No agent does work outside its tier. Communication flows via a Redis pub/sub
message bus using structured JSON packets.

```
┌─────────────────────────────────────────────────────────────────┐
│  TIER 0 — CONTEXT (Market Regime)                                │
│   The Pulse    ──►  regime + bias + conviction floors            │
├─────────────────────────────────────────────────────────────────┤
│  TIER 1 — SENSORS (Data Ingest)                                 │
│   The Scanner  ──►  market data packets (100–500ms)             │
│   The Wire     ──►  catalyst tags + sentiment scores            │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│  TIER 2 — ANALYSTS (Signal Generation)                          │
│   The Chartist ──►  setup packets with quality score            │
│   The Actuary  ──►  risk approval or veto                       │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│  TIER 3 — DECISION                                              │
│   The General  ──►  trade directive (enter / pass / wait)       │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│  TIER 4 — EXECUTION                                             │
│   The Sniper   ──►  order to broker API                         │
│   The Watcher  ──►  exit orders, trailing stop updates          │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│  TIER 5 — MEMORY & LEARNING                                     │
│   The Historian ──►  performance weights back to The General    │
└─────────────────────────────────────────────────────────────────┘
```

---

## Shared Infrastructure

### Message Bus

All inter-agent communication uses Redis pub/sub.
Every agent publishes structured JSON to a named channel.
Only The General can publish to `scalpbot:directives`.

### Shared State Store

Every agent has **read** access to the shared state store.
Write access is scoped per the table below.

| Key                        | Type    | Writer         | Description                        |
|----------------------------|---------|----------------|------------------------------------|
| `state:market:{ticker}`    | hash    | The Scanner    | Latest L1/L2 snapshot per ticker   |
| `state:catalyst:{ticker}`  | hash    | The Wire       | Latest catalyst tag + sentiment    |
| `state:setups`             | list    | The Chartist   | Active setup packets               |
| `state:risk-params`        | hash    | The Actuary    | Current sizing limits, halt flag   |
| `state:positions`          | hash    | The Sniper     | Open positions keyed by ticker     |
| `state:pending-orders`     | hash    | The Sniper     | Pending order IDs                  |
| `state:daily-pnl`          | float   | The Watcher    | Realized P&L for the session       |
| `state:trade-log`          | stream  | The Historian  | Append-only trade record           |
| `state:setup-weights`      | hash    | The Historian  | Per-setup performance weights      |
| `state:market-regime`      | string  | The Pulse      | JSON regime + bias + floors        |
| `state:watchlist`          | string  | UI / API       | JSON list of user-added tickers    |

### Circuit Breaker

The Actuary publishes `state:risk-params.halted = true` when the daily drawdown
limit is breached. Every agent must check this flag before acting. The General
will not emit directives while halted. The Sniper will not place orders while halted.

---

## Tier 0 — Context

### The Pulse (Market Regime Agent)

**Personality:** A weather station — impersonal, always broadcasting, never trading.

**Role:** Read SPY, QQQ, and VIX-proxy (UVXY) prices from market data. Compute
the current market regime and directional bias. Publish regime context that the
Actuary and General consume to make market-aware decisions.

**Tools:**
- Shared state store (read `state:market:*`)
- Pure Python signal processing (no LLM calls)

**Subscribes to:** `scalpbot:market-data`

**Publishes to:** `scalpbot:market-regime`

**Payload schema:**

```json
{
  "regime": "BEARISH",
  "bias": "SHORT_PREFERRED",
  "long_conviction_floor": 8,
  "short_conviction_floor": 5,
  "vix_regime": "elevated",
  "vix": 27.3,
  "spy_change_pct": -0.82,
  "qqq_change_pct": -1.14,
  "spy_price": 547.20,
  "qqq_price": 462.80,
  "note": "BEARISH tape — SPY -0.8%, QQQ -1.1%. Prefer shorts, longs need 8+ conviction.",
  "updated_at_ms": 1711234567890
}
```

**Regime states:** `BULLISH`, `BEARISH`, `CHOPPY`, `RISK_OFF`

**Bias values:** `LONG_PREFERRED`, `SHORT_PREFERRED`, `NEUTRAL`, `NO_NEW_LONGS`

**State writes:** `state:market-regime`

**Rules:**
- Does NOT make LLM calls — pure Python rule-based (HMM upgrade planned).
- Publishes regime every 60 seconds (configurable).
- RISK_OFF regime blocks all new positions at the Actuary level.
- BEARISH regime raises the long conviction floor to 8.
- The General applies regime-adjusted conviction penalties to contra-trend trades.
- The Watcher uses end-of-day liquidation at 3:45 PM ET to prevent overnight positions.

---

## Tier 1 — Sensors

### The Scanner (Market Data Agent)

**Personality:** Cold, machine-like, emotionless. No opinions — only facts.

**Role:** Ingest real-time L1/L2 order book data, tape prints, and volume deltas.
Surface anomalies and publish structured market packets every 100–500ms.

**Tools:**
- WebSocket feed from broker or Polygon/Alpaca
- Direct L2 order book subscription

**Publishes to:** `scalpbot:market-data`

**Payload schema:**

```json
{
  "ticker": "AAPL",
  "timestamp_ms": 1711234567890,
  "bid": 182.10,
  "ask": 182.12,
  "spread": 0.02,
  "last": 182.11,
  "volume_delta": 142300,
  "tape_anomaly": false,
  "sweep_detected": false,
  "unusual_size_bid": false,
  "unusual_size_ask": true
}
```

**State writes:** `state:market:{ticker}`

**Rules:**
- Never interpret data — only report it.
- Publish even if no anomaly is detected. Downstream agents need the heartbeat.
- Drop packets older than 1 second. Stale data is worse than no data.

---

### The Wire (News & Sentiment Agent)

**Personality:** A Bloomberg terminal analyst — fast, terse, source-conscious.

**Role:** Monitor news feeds, SEC filings, and social flow. Tag catalysts with
sentiment scores and confidence levels.

**Tools:**
- NewsAPI, Benzinga, Reuters feeds
- SEC EDGAR real-time feed
- Twitter/X filtered stream

**Publishes to:** `scalpbot:catalysts`

**Payload schema:**

```json
{
  "ticker": "AAPL",
  "timestamp_ms": 1711234567890,
  "catalyst_type": "earnings_beat",
  "headline": "Apple Q2 beats by $0.18 EPS",
  "sentiment": "positive",
  "sentiment_score": 0.87,
  "confidence": 0.92,
  "source": "benzinga",
  "time_sensitivity": "high"
}
```

**State writes:** `state:catalyst:{ticker}`

**Catalyst types:** `earnings_beat`, `earnings_miss`, `upgrade`, `downgrade`,
`fda_approval`, `fda_rejection`, `macro_print`, `social_spike`, `sec_filing`

**Rules:**
- Tag every catalyst with a confidence level. The General weights low-confidence
  catalysts differently.
- Time-sensitive catalysts (`time_sensitivity: high`) must publish within 2 seconds
  of source event.
- Do not fabricate or infer catalysts. Source-based facts only.

---

## Tier 2 — Analysts

### The Chartist (Technical Analysis Agent)

**Personality:** A seasoned tape reader — opinionated but data-driven, never emotional.

**Role:** Consume market data packets and compute real-time technical indicators.
Detect trade setups and score their quality.

**Tools:**
- Shared state store (read `state:market:*`)
- TA-Lib or pandas-ta for indicator calculations
- Indicator config from `config/indicators.yaml`

**Subscribes to:** `scalpbot:market-data`

**Publishes to:** `scalpbot:setups`

**Payload schema:**

```json
{
  "setup_id": "uuid-v4",
  "ticker": "AAPL",
  "timestamp_ms": 1711234567890,
  "setup_type": "vwap_reclaim",
  "direction": "long",
  "quality_score": 8,
  "entry_zone": [182.05, 182.20],
  "suggested_stop": 181.40,
  "suggested_target": 183.60,
  "signals": {
    "vwap": "reclaim_confirmed",
    "ema_5": "above",
    "ema_9": "above",
    "rsi": 38,
    "volume_confirmation": true
  }
}
```

**State writes:** `state:setups`

**Setup types:** `vwap_reclaim`, `ema_bounce`, `hod_breakout`, `momentum_continuation`,
`tape_sweep_follow`, `float_rotation`

**Quality score (1–10):**
- 9–10: All signals aligned, high-volume confirmation, clear catalyst
- 7–8: Strong setup, minor signal gap
- 5–6: Borderline — The General may pass or wait
- 1–4: Do not publish. Filter internally.

**Rules:**
- Never publish a setup with quality score below 5.
- Always include at least 3 signals in the `signals` object.
- Recalculate indicators on every market data packet for active tickers.

---

### The Actuary (Risk Modeling Agent)

**Personality:** A paranoid risk officer — skeptical, conservative, always thinking
worst-case.

**Role:** Evaluate every setup against current portfolio exposure. Calculate
position size. Veto any trade that breaches risk thresholds. Trigger daily halt
if drawdown limit is hit.

**Tools:**
- Shared state store (read all keys)
- Risk config from `config/risk-params.yaml`
- Kelly fraction calculator

**Subscribes to:** `scalpbot:setups`, `scalpbot:fill-reports`

**Publishes to:** `scalpbot:risk-decisions`

**Payload schema — approval:**

```json
{
  "setup_id": "uuid-v4",
  "decision": "approved",
  "approved_shares": 150,
  "stop_price": 181.40,
  "max_loss_dollars": 105.00,
  "position_heat_score": 3,
  "portfolio_heat_pct": 1.8
}
```

**Payload schema — veto:**

```json
{
  "setup_id": "uuid-v4",
  "decision": "vetoed",
  "reason": "portfolio_heat_exceeded",
  "current_heat_pct": 4.2,
  "max_heat_pct": 4.0
}
```

**State writes:** `state:risk-params`

**Veto conditions (any one triggers veto):**

| Condition                      | Threshold              |
|--------------------------------|------------------------|
| Daily drawdown                 | -3% of account         |
| Position heat (single trade)   | > 2% of portfolio      |
| Portfolio heat (all open)      | > 4% of portfolio      |
| Existing position in ticker    | Any open position      |
| Max open positions             | > 4 concurrent         |
| Max total notional             | > 60% of account       |
| Market regime RISK_OFF         | Blocks all new trades  |
| Bearish regime + low quality   | Long needs quality >= 8|
| High-volatility halt flag      | VIX > 35 or manual set |
| Setup quality below threshold  | quality_score < 6      |

**Daily halt:** When daily drawdown hits -3%, The Actuary publishes
`state:risk-params.halted = true` and emits a halt event to `scalpbot:risk:daily-halt`.
**This veto cannot be overridden by any other agent or by Cliff's manual input
during a live session.** Reset requires a new session start.

**Rules:**
- The Actuary has permanent veto authority. No other agent can override it.
- Every setup must receive either an `approved` or `vetoed` decision before
  The General can act on it.
- Position sizing uses ATR-based stops × account risk %, capped by Kelly fraction.
- Sizing calculations must be logged to `artifacts/decisions/` for audit.

---

## Tier 3 — Decision

### The General (Trade Coordinator Agent)

**Personality:** A calm, decisive military commander — synthesizes inputs, breaks
ties, owns the final call.

**Role:** The only agent that can emit trade directives. Synthesize setup quality,
catalyst strength, risk approval, time-of-day, and current P&L to decide: enter,
pass, or wait for re-entry.

**Tools:**
- Shared state store (read all keys)
- Setup weights from `state:setup-weights` (fed by The Historian)
- Time-of-day config from `config/session.yaml`

**Subscribes to:** `scalpbot:setups`, `scalpbot:risk-decisions`, `scalpbot:catalysts`

**Publishes to:** `scalpbot:directives`

**Payload schema:**

```json
{
  "directive_id": "uuid-v4",
  "setup_id": "uuid-v4",
  "ticker": "AAPL",
  "action": "enter",
  "direction": "long",
  "entry_price": 182.15,
  "stop_price": 181.40,
  "target_price": 183.60,
  "approved_shares": 150,
  "time_in_force": "day",
  "conviction_score": 8.4,
  "rationale": "VWAP reclaim on earnings catalyst, risk approved, high-volume confirmation"
}
```

**Action values:** `enter`, `pass`, `wait`

**Conviction score formula:**
```
conviction = (setup_quality × 0.4) + (catalyst_score × 0.3) + (setup_weight × 0.2) + (time_of_day_bonus × 0.1)
```

**Enter threshold:** conviction_score ≥ 7.0

**Rules:**
- Will not emit directives if `state:risk-params.halted = true`.
- Will not emit a directive for a setup that has not received an `approved` risk decision.
- Deduplicates on `setup_id` — will never emit two directives for the same setup.
- Preferred trading windows: first 90 minutes and last 60 minutes of session.
  Outside these windows, raise the enter threshold to 8.5.
- Passes setups with catalyst `time_sensitivity: high` faster — skip the wait action.

---

## Tier 4 — Execution

### The Sniper (Order Execution Agent)

**Personality:** Robotic, fast, zero hesitation. Built for speed, not thinking.

**Role:** Receive trade directives from The General and fire orders to the broker API.
Use smart order routing to minimize slippage. Report fill quality.

**Tools:**
- Broker REST/WebSocket API (IBKR, Alpaca)
- Smart order routing config from `config/execution.yaml`

**Subscribes to:** `scalpbot:directives`

**Publishes to:** `scalpbot:fill-reports`

**Payload schema — fill report:**

```json
{
  "directive_id": "uuid-v4",
  "order_id": "broker-12345",
  "ticker": "AAPL",
  "status": "filled",
  "filled_shares": 150,
  "avg_fill_price": 182.17,
  "slippage_cents": 2,
  "timestamp_ms": 1711234567890
}
```

**Fill status values:** `filled`, `partial`, `rejected`, `cancelled`

**State writes:** `state:positions`, `state:pending-orders`

**Order routing strategy:**
- Longs: limit order at ask price, peg to market if not filled within 500ms
- Shorts: limit order at bid price, peg to market if not filled within 500ms
- Large size (> $10k notional): time-slice into 3 child orders over 2 seconds

**Rules:**
- Only place orders for directives with a valid `directive_id`. Reject anything else.
- Never call the broker API for any purpose other than executing or cancelling orders.
  All other broker interactions belong in The Watcher.
- If a fill is partial after 2 seconds, cancel the remainder and report partial fill.
- Never retry a rejected order without a new directive from The General.

---

### The Watcher (Position Monitor Agent)

**Personality:** A nervous, hyper-vigilant sentinel. Never stops watching.

**Role:** Track every open position in real time. Trail stops as trades move in
favor. Detect deteriorating tape and reversal signals. Trigger exits immediately
when conditions are met.

**Tools:**
- Shared state store (read `state:positions`, `state:market:*`)
- Broker WebSocket for real-time position P&L
- Exit config from `config/exit-rules.yaml`

**Subscribes to:** `scalpbot:fill-reports`, `scalpbot:market-data`

**Publishes to:** `scalpbot:exit-orders`

**Payload schema — exit order:**

```json
{
  "directive_id": "uuid-v4",
  "ticker": "AAPL",
  "exit_type": "trailing_stop_triggered",
  "shares": 150,
  "order_type": "market",
  "reason": "Price dropped below trailing stop at 182.40"
}
```

**Exit types:** `target_hit`, `stop_hit`, `trailing_stop_triggered`,
`tape_reversal`, `time_exit`, `risk_halt`

**State writes:** `state:daily-pnl`

**Trailing stop logic:**
- Activate trailing stop once position is up 50% of the way to target.
- Trail stop distance = ATR × 1.5, recalculated every market data packet.
- Never widen a trailing stop. Only tighten or hold.

**Rules:**
- Monitor every open position on every market data packet. No exceptions.
- Time exits: if a position has not hit stop or target within 15 minutes, exit at market.
- On `state:risk-params.halted = true`, immediately exit all open positions at market.
- Publish exit orders to `scalpbot:exit-orders`. The Sniper executes them.

---

## Tier 5 — Memory & Learning

### The Historian (Performance Agent)

**Personality:** A dispassionate statistician. Numbers only, no narratives.

**Role:** Log every trade. Calculate per-setup win rates, expectancy, and drawdown.
Feed performance weights back to The General so higher-performing setups get
prioritized. Flag setup degradation.

**Tools:**
- Write access to `state:trade-log` (append-only Redis stream)
- Write access to `state:setup-weights`
- Time-series database (TimescaleDB or InfluxDB) for historical analysis
- Stats config from `config/performance.yaml`

**Subscribes to:** `scalpbot:fill-reports`, `scalpbot:exit-orders`

**Publishes to:** `scalpbot:performance-updates`

**Payload schema — performance update:**

```json
{
  "setup_type": "vwap_reclaim",
  "sample_size": 47,
  "win_rate": 0.62,
  "avg_win_cents": 22.4,
  "avg_loss_cents": 11.8,
  "expectancy_cents": 9.4,
  "weight": 0.78,
  "degradation_flag": false
}
```

**State writes:** `state:trade-log`, `state:setup-weights`

**Weight update formula:**
```
weight = (win_rate × avg_win) / ((1 - win_rate) × avg_loss)
# Normalized to [0.0, 1.0]. Minimum 20 sample trades before weight is applied.
```

**Degradation flag:** Set `degradation_flag: true` when the rolling 20-trade
win rate drops more than 15 percentage points below the all-time win rate for
that setup type.

**Rules:**
- Trade log is append-only. Never modify or delete existing entries.
- Do not update weights until a setup type has at least 20 completed trades.
- Emit a performance update after every completed trade.
- Write every trade to both the Redis stream and the time-series database.
- Logs stay local — never commit `artifacts/trades/` to git.

---

## Channel Directory

| Channel                      | Publisher        | Subscribers                                   |
|------------------------------|------------------|-----------------------------------------------|
| `scalpbot:market-data`       | The Scanner      | The Chartist, The Watcher, The Pulse          |
| `scalpbot:market-regime`     | The Pulse        | The General                                   |
| `scalpbot:catalysts`         | The Wire         | The General                                   |
| `scalpbot:setups`            | The Chartist     | The Actuary, The General                      |
| `scalpbot:risk-decisions`    | The Actuary      | The General                                   |
| `scalpbot:directives`        | The General      | The Sniper                                    |
| `scalpbot:fill-reports`      | The Sniper       | The Actuary, The Watcher, The Historian       |
| `scalpbot:exit-orders`       | The Watcher      | The Sniper, The Historian                     |
| `scalpbot:performance-updates` | The Historian  | The General                                   |
| `scalpbot:risk:daily-halt`   | The Actuary      | The General, The Sniper, The Watcher          |

---

## Config Files

| File                          | Owner            | Contents                                     |
|-------------------------------|------------------|----------------------------------------------|
| `config/indicators.yaml`      | The Chartist     | EMA periods, RSI window, ATR period, VWAP    |
| `config/risk-params.yaml`     | The Actuary      | Drawdown limits, heat %, Kelly cap           |
| `config/execution.yaml`       | The Sniper       | Order routing rules, peg timeout, slice size |
| `config/exit-rules.yaml`      | The Watcher      | Trailing stop multiplier, time exit window   |
| `config/session.yaml`         | The General      | Trading hours, conviction thresholds         |
| `config/performance.yaml`     | The Historian    | Min sample size, degradation threshold       |

No secrets live in config files. API keys go in `.env` (gitignored).

---

## Source File Map

```
src/
├── agents/
│   ├── market_data_agent.py        ← The Scanner
│   ├── market_pulse_agent.py       ← The Pulse (NEW)
│   ├── news_sentiment_agent.py     ← The Wire
│   ├── technical_analysis_agent.py ← The Chartist
│   ├── risk_modeling_agent.py      ← The Actuary
│   └── performance_agent.py        ← The Historian
├── coordinator/
│   └── trade_coordinator.py        ← The General
└── execution/
    ├── order_execution_agent.py    ← The Sniper
    └── position_monitor_agent.py   ← The Watcher
```

---

*Update this file whenever a channel name, payload field, or state key changes.
Do not merge without updating this file if any communication contract changed.*

Last updated: 2026-03-26 — ScalpBot / CliffClaw v1.1 (Pulse agent, guardrails, regime awareness)
