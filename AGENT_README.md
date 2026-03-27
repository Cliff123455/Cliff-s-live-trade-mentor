# ScalpBot // CliffClaw — Agent Reference

A five-tier, eight-agent autonomous scalping system. Each agent has a fixed role, communicates via Redis pub/sub, and reads its config from a dedicated YAML file.

---

## Architecture Overview

```
Tier 1: PERCEPTION          Tier 2: ANALYSIS         Tier 3: DECISION
+---------------+          +----------------+        +---------------+
| The Scanner   |--data--->| The Chartist   |--setup->| The General   |
| The Wire      |--news--->| The Actuary    |--risk-->|               |
+---------------+          +----------------+        +-------+-------+
                                                             |
                                                         directive
                                                             |
                           Tier 4: EXECUTION          Tier 5: MEMORY
                           +----------------+        +---------------+
                           | The Sniper     |--fill->| The Historian |
                           | The Watcher    |--exit->|               |
                           +----------------+        +---------------+
```

---

## Tier 1: Perception

### The Scanner (Market Data Agent)

| Field | Value |
|---|---|
| **File** | `src/agents/market_data_agent.py` |
| **Config** | `config/indicators.yaml` |
| **Default Model** | `google/gemini-2.0-flash-001` |
| **Publishes to** | `scalpbot:market-data` |
| **Subscribes to** | Nothing (source agent) |

**Role:** Dynamically screens the market for the most active stocks and streams live quote/trade data into the pipeline.

**How it works:**
1. Calls Alpaca's Most Actives screener to find the top candidates by trade count
2. Filters by price range ($10-$90) and minimum trade count
3. Takes the top 20 and opens a live WebSocket stream for quotes and trades
4. Refreshes the screener every 30 minutes and reconnects the stream
5. Falls back to a hardcoded watchlist if the screener fails or returns < 5 results

**Criteria for stock selection:**
- Price between `$10.00` and `$90.00` (configurable: `screener.min_price`, `screener.max_price`)
- Minimum trade count of `10,000` today (configurable: `screener.min_trade_count`)
- Sorted by trade count descending, top 20 selected (configurable: `screener.top_n`)
- If screener is disabled or fails, uses `fallback_watchlist` from config

**Output per tick:** ticker, bid, ask, spread, last trade price, volume delta, unusual size flags, sweep detection flags.

---

### The Wire (News & Sentiment Agent)

| Field | Value |
|---|---|
| **File** | `src/agents/news_sentiment_agent.py` |
| **Config** | `config/indicators.yaml` (watchlist, X accounts) |
| **Default Model** | `google/gemini-2.0-flash-001` |
| **Publishes to** | `scalpbot:catalysts` |
| **Subscribes to** | Nothing (source agent) |

**Role:** Monitors three news/social sources in parallel and classifies each item via LLM for market impact.

**Three concurrent loops:**

1. **Alpaca News** (every 30s) — Fetches up to 5 recent articles per watchlist ticker from the last 24 hours. Each article is sent to the LLM for classification.

2. **Trump Truth Social** (every 2min) — Polls @realDonaldTrump's public feed. LLM classifies each post for market relevance, affected tickers/sectors, and catalyst type (tariff, trade_deal, sanction, fed_comment, crypto_mention, etc.).

3. **X / Twitter** (every 60s) — Searches for cashtag mentions of watchlist tickers and posts from configured accounts. Requires `X_BEARER_TOKEN` env var.

**LLM classification output per item:**
- `catalyst_type`: earnings_beat, earnings_miss, upgrade, downgrade, fda_approval, fda_rejection, macro_print, social_spike, sec_filing, none
- `sentiment`: positive / negative / neutral
- `sentiment_score`: 0.0 to 1.0
- `confidence`: 0.0 to 1.0
- `time_sensitivity`: immediate / high / medium / low

**Criteria for publishing:** Every new (unseen) article/post with a non-empty headline is classified and published. No minimum score filter — The General decides relevance downstream.

---

## Tier 2: Analysis

### The Chartist (Technical Analysis Agent)

| Field | Value |
|---|---|
| **File** | `src/agents/technical_analysis_agent.py` |
| **Config** | `config/indicators.yaml` |
| **Default Model** | `anthropic/claude-3.5-sonnet` |
| **Publishes to** | `scalpbot:setups` |
| **Subscribes to** | `scalpbot:market-data` |

**Role:** Consumes live market data, computes technical indicators on rolling 1-minute bars, detects scalping setups, scores them 1-10, and publishes qualified setups.

**Indicators computed:**
- EMA 5, 9, 21 (configurable periods)
- RSI 14 (oversold: 35, overbought: 65)
- ATR 14
- Session VWAP (cumulative from first bar)

**Setup detectors and their criteria:**

| Setup | Direction | Entry Criteria | Score Range |
|---|---|---|---|
| **VWAP Reclaim** | Long | Price crossed from below to above VWAP + RSI < 50 + volume > 1.5x average | 6-8 |
| **EMA Bounce** | Long | Price touched EMA9 (low <= EMA9), closed above it, EMAs aligned 5>9>21 | 6-8 |
| **EMA Bounce** | Short | Price touched EMA9 (high >= EMA9), closed below it, EMAs aligned 5<9<21 | 6-8 |
| **HOD Breakout** | Long | Close above session high-of-day + volume confirmed | 7-9 |
| **Momentum Continuation** | Long | RSI 50-65 + EMAs aligned 5>9>21 + volume spike | 6-8 |

**Quality scoring bonuses:**
- RSI in favorable zone: +1
- Volume significantly above average (1.5x the confirmation threshold): +1
- EMAs well-spread (nicely fanned): +1
- All scores capped at 10

**Gates before publishing:**
1. Minimum `21` bars of data (longest EMA period) before indicators compute
2. Quality score must meet `min_publish_quality` (default: **5**)
3. Same ticker + setup type suppressed for 60 seconds (dedup)
4. Indicators recomputed at most once per second per ticker (throttle)

**Each published setup includes:** setup_id, ticker, setup_type, direction, quality_score, entry_zone [bid, ask], suggested_stop (entry - 1.5x ATR), suggested_target (entry + 4.5x ATR for 3:1 R/R), signal details.

---

### The Actuary (Risk Modeling Agent)

| Field | Value |
|---|---|
| **File** | `src/agents/risk_modeling_agent.py` |
| **Config** | `config/risk-params.yaml` |
| **Default Model** | `anthropic/claude-3.5-sonnet` |
| **Publishes to** | `scalpbot:risk-decisions` |
| **Subscribes to** | `scalpbot:setups`, `scalpbot:fill-reports` |

**Role:** Gates every setup through a 9-checkpoint risk pipeline. Sizes positions. Monitors daily P&L. Triggers the session halt. **Its veto is final and cannot be overridden.**

**9 Risk Gates (in order):**

| Gate | Check | Veto Reason |
|---|---|---|
| 1 | Session halted? | `session_halted` |
| 2 | Quality score >= 6? | `quality_below_minimum` |
| 3 | Load current positions from Redis + Alpaca | (data load) |
| 4 | Ticker already has an open position? | `position_already_open` |
| 5 | Entry price and stop price are valid (> 0)? | `invalid_price_data` |
| 6 | Position sizing produces > 0 shares? | `zero_shares_computed` |
| 7 | Single position heat <= 2% of account? | `position_heat_exceeded` |
| 8 | Total portfolio heat <= 4% of account? | `portfolio_heat_exceeded` |
| 9 | Risk/Reward ratio >= 2.0? | `rr_ratio_too_low` |

**Position sizing (ATR method):**
- `max_risk_dollars = account_value * (max_loss_per_trade_pct / 100)`
- `shares = max_risk_dollars / stop_distance`
- Hard cap: 10,000 shares max
- Notional cap: respects `max_position_dollars` from UI (default 20% of account)

**Daily halt trigger:**
- Monitors P&L every 30 seconds
- If daily loss exceeds `daily_drawdown_halt_pct` (default: **3%**), sets `halted = true` in Redis
- Halt is **permanent for the session** — no new trades until next day
- Publishes halt event on `scalpbot:risk:daily-halt`

**Configurable thresholds (from config or UI override):**
- `max_position_heat_pct`: 2.0% (single trade risk)
- `max_portfolio_heat_pct`: 4.0% (total open risk)
- `min_rr_ratio`: 2.0 (minimum reward-to-risk)
- `min_quality_score`: 6 (Chartist score gate)
- `daily_drawdown_halt_pct`: 3.0% (session kill switch)
- `vix_halt_level`: 35.0 (VIX circuit breaker)

---

## Tier 3: Decision

### The General (Trade Coordinator)

| Field | Value |
|---|---|
| **File** | `src/coordinator/trade_coordinator.py` |
| **Config** | `config/session.yaml` |
| **Default Model** | `anthropic/claude-opus-4-5` |
| **Publishes to** | `scalpbot:directives` |
| **Subscribes to** | `scalpbot:setups`, `scalpbot:risk-decisions`, `scalpbot:catalysts`, `scalpbot:performance-updates`, `scalpbot:risk:daily-halt` |

**Role:** The only agent authorized to issue trade directives. Synthesizes setup quality, catalyst sentiment, historical performance weights, and time-of-day into a single conviction score. Calls the LLM for borderline decisions.

**Guard rails (checked before scoring):**
1. Session halted? -> pass
2. Outside trading hours (session_open to session_close)? -> pass
3. Max directives per session reached (20)? -> pass
4. Ticker in cooldown (300s since last directive on same ticker)? -> pass

**Conviction score formula (0-10 scale):**
```
conviction = (
    (quality_score / 10) * 0.40          # setup quality from Chartist
  + catalyst_sentiment_score * 0.30       # from The Wire (default 0.65 if no data)
  + setup_performance_weight * 0.20       # from The Historian (default 0.50)
  + time_of_day_bonus * 0.10             # 1.0 if in prime window, else 0.0
) * 10
```

**Decision logic:**
- Score >= threshold (default **5.5**, UI override via `conviction_threshold`): **ENTER**
- Score < threshold: **PASS**
- Score between 6.5 and 7.5 (borderline): **LLM decides** — sends full context to Claude Opus and asks "Should I enter this trade?"

**Prime windows** (lower effective threshold via time_of_day_bonus):
- Morning open: 09:30 - 11:00 ET
- Power hour: 15:00 - 20:00 ET

**Session hours:** 09:30 - 20:00 ET (extended for paper; revert to 16:00 for live)

---

## Tier 4: Execution

### The Sniper (Order Execution Agent)

| Field | Value |
|---|---|
| **File** | `src/execution/order_execution_agent.py` |
| **Config** | `config/execution.yaml` |
| **Default Model** | `google/gemini-2.0-flash-001` |
| **Publishes to** | `scalpbot:fill-reports` |
| **Subscribes to** | `scalpbot:directives`, `scalpbot:exit-orders` |

**Role:** Executes trade directives via Alpaca with zero hesitation. Handles entries and exits.

**Entry execution flow:**
1. Receives ENTER directive from The General
2. Checks halt status (won't execute if halted)
3. Validates ticker, shares > 0, entry_price > 0
4. If notional > $10,000: **time-slices** into 3 child orders with 0.7s delay between slices
5. Otherwise: places a **limit order** at the directive's entry price
6. Waits 500ms for fill
7. If not filled: **cancels limit and places market order** (fallback)
8. Publishes fill report with actual fill price, slippage, and shares filled

**Exit execution:** Receives exit orders from The Watcher, places market orders to close.

**Key settings:**
- Entry order type: `limit` (with market fallback after 500ms)
- Large order threshold: `$10,000` notional
- Time-in-force: `day`
- Hard cap check: respects halt status before executing

---

### The Watcher (Position Monitor Agent)

| Field | Value |
|---|---|
| **File** | `src/execution/position_monitor_agent.py` |
| **Config** | `config/exit-rules.yaml` |
| **Default Model** | `google/gemini-2.0-flash-001` |
| **Publishes to** | `scalpbot:exit-orders` |
| **Subscribes to** | `scalpbot:fill-reports`, `scalpbot:market-data`, `scalpbot:risk:daily-halt` |

**Role:** Hyper-vigilant sentinel that monitors every open position tick-by-tick and triggers exits.

**Exit checks (evaluated on every market data tick, in order):**

| Check | Trigger Condition | Exit Type |
|---|---|---|
| 1. Time exit | Position open > 30 minutes (configurable) | `time_exit` |
| 2. Target hit | Price reaches target (disabled by default — lets winners run) | `target_hit` |
| 3. Trailing stop activation | Gain >= 0.5% from entry | Activates trailing stop |
| 4. Trailing stop trigger | Price drops 0.25% from highest point after activation | `trailing_stop_triggered` |
| 5. Hard stop | Price hits the initial stop set by Actuary/Chartist | `stop_hit` |

**Trailing stop mechanics:**
- **Activation:** Once price moves 0.5% in the trade's favor (configurable via UI: `Trail Activates At`)
- **Trail distance:** 0.25% behind highest favorable price (configurable via UI: `Trail Distance`)
- Trail only ratchets in favor — never moves against the trade
- Works for both long and short positions

**Daily halt response:** On halt signal from The Actuary, immediately exits ALL open positions with market orders.

**Background poll loop:** Checks time exits every 1 second even when no market data arrives.

**All exit settings are overridable from the dashboard in real time** via Redis `state:trade-rules`.

---

## Tier 5: Memory

### The Historian (Performance Agent)

| Field | Value |
|---|---|
| **File** | `src/agents/performance_agent.py` |
| **Config** | `config/performance.yaml` |
| **Default Model** | `anthropic/claude-haiku-4-5-20251001` |
| **Publishes to** | `scalpbot:performance-updates` |
| **Subscribes to** | `scalpbot:fill-reports`, `scalpbot:exit-orders` |

**Role:** Logs every completed trade to SQLite, calculates per-setup-type statistics, and feeds performance weights back to The General. Does NOT make LLM calls.

**What it tracks per trade:**
- directive_id, setup_id, ticker, setup_type, direction
- entry_price, exit_price, shares
- entry/exit timestamps, P&L, exit_reason

**Weight calculation (Kelly-inspired):**
```
win_rate = wins / total_trades
weight = (win_rate * avg_win) / ((1 - win_rate) * avg_loss)
Clamped to [0.0, 1.0]
```
- Requires minimum **20** completed trades per setup type before weight is applied
- Weight is published to Redis so The General can use it in conviction scoring

**Degradation detection:**
- Compares rolling 20-trade win rate to all-time win rate for each setup type
- Flags a setup if the rolling win rate drops more than **15 percentage points** below all-time
- The General can use this flag to avoid degrading strategies

**Persistence:**
- SQLite database at `data/trades.db`
- Redis stream `state:trade-log` (append-only)
- Redis hash `state:setup-weights` (The General reads on startup)
- Performance updates published every **30 seconds** for all tracked setup types

---

## Data Flow Summary

```
Scanner --(market-data)--> Chartist --(setups)--> Actuary --(risk-decisions)--> General
Wire ----(catalysts)-----> General                                                 |
Historian -(perf-updates)-> General                                                |
                                                                              (directives)
                                                                                   |
                                                                                   v
                                                                               Sniper
                                                                                   |
                                                                            (fill-reports)
                                                                                   |
                                                          +------------------------+
                                                          |                        |
                                                          v                        v
                                                       Watcher               Historian
                                                          |
                                                     (exit-orders)
                                                          |
                                                          v
                                                       Sniper (executes exit)
```

---

## Dashboard UI Controls

All of these are live-editable during trading and override config file values via Redis:

| Control | Redis Key | Default | Effect |
|---|---|---|---|
| Max Position ($) | `state:trade-rules.max_position_dollars` | 20000 | Notional cap per trade |
| Max Loss/Trade (%) | `state:trade-rules.max_loss_per_trade_pct` | 0.5 | Risk per position |
| Max Daily Loss (%) | `state:trade-rules.max_daily_loss_pct` | 2.0 | Session halt threshold |
| Trail Activates At (%) | `state:trade-rules.trailing_stop_activation_pct` | 0.5 | When trailing stop kicks in |
| Trail Distance (%) | `state:trade-rules.trailing_stop_trail_pct` | 0.25 | How tight the trail follows |
| Conviction Threshold | `state:trade-rules.conviction_threshold` | 6.0 | Minimum score to enter |
| Time Exit (min) | `state:trade-rules.time_exit_minutes` | 30 | Max position hold time |

---

## Default AI Models

| Agent | Default Model | Why |
|---|---|---|
| The Scanner | `google/gemini-2.0-flash-001` | Fast, cheap — just streaming data |
| The Wire | `google/gemini-2.0-flash-001` | Fast classification of news items |
| The Chartist | `anthropic/claude-3.5-sonnet` | Accurate technical pattern recognition |
| The Actuary | `anthropic/claude-3.5-sonnet` | Precise risk calculations |
| The General | `anthropic/claude-opus-4-5` | Best judgment for trade decisions |
| The Sniper | `google/gemini-2.0-flash-001` | Speed — no LLM calls during execution |
| The Watcher | `google/gemini-2.0-flash-001` | Speed — no LLM calls during monitoring |
| The Historian | `anthropic/claude-haiku-4-5-20251001` | Lightweight — no LLM calls, just logging |

Models are swappable per agent from the dashboard (Agent Models section) without restarting.
