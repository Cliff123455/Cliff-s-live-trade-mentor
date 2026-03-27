# ScalpBot // CliffClaw

An autonomous intraday scalping system built on a five-tier, eight-agent architecture. Each agent has a single job. They communicate through a Redis message bus, never calling each other directly. A Flask dashboard shows everything in real time.

> **Paper trading only by default.** All orders go to Alpaca's paper environment until you change `ALPACA_BASE_URL` in `.env`.

---

## Quick Start

1. Copy `.env.example` to `.env` and fill in your API keys (Alpaca + OpenRouter).
2. Double-click `start.bat`.

That's it. The launcher will:
- Start Docker Desktop if it isn't running
- Spin up a Redis container
- Create a Python virtual environment and install dependencies
- Open the dashboard at `http://127.0.0.1:5000`
- Wait for you to click **Launch Agents**

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  TIER 1 — PERCEPTION                                            │
│  The Scanner  ──→  scalpbot:market-data                         │
│  The Wire     ──→  scalpbot:catalysts                           │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  TIER 2 — ANALYSIS                                              │
│  The Chartist ──→  scalpbot:setups                              │
│  The Actuary  ──→  scalpbot:risk-decisions                      │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  TIER 3 — DECISION                                              │
│  The General  ──→  scalpbot:directives                          │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  TIER 4 — EXECUTION                                             │
│  The Sniper   ──→  scalpbot:fill-reports                        │
│  The Watcher  ──→  scalpbot:exit-orders                         │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  TIER 5 — MEMORY                                                │
│  The Historian ──→  scalpbot:performance-updates                │
└─────────────────────────────────────────────────────────────────┘
```

---

## The Eight Agents

### Tier 1 — Perception

**The Scanner** `src/agents/market_data_agent.py`
Streams live quotes and trades from Alpaca for every ticker on the watchlist. Calculates VWAP and rolling volume in real time. Publishes a market-data event on every price tick. Every downstream agent depends on this feed being alive.

**The Wire** `src/agents/news_sentiment_agent.py`
Runs two parallel loops:
- Polls the Alpaca news API every 30 seconds for each watchlist ticker.
- Polls Trump's Truth Social account every 2 minutes via the public Mastodon-compatible API.

Each article or post is sent to the LLM, which classifies it: catalyst type, sentiment, sentiment score, confidence, and time sensitivity. The result is published to `scalpbot:catalysts`. Trump posts tagged as `trump_*` catalyst types also identify affected tickers and sectors.

---

### Tier 2 — Analysis

**The Chartist** `src/agents/technical_analysis_agent.py`
Listens to the market-data feed. Loads 60 bars of 1-minute history at session open, then maintains a live rolling bar buffer per ticker. Calculates EMA (5/9/21), RSI, ATR, and VWAP. When a pattern fires — EMA bounce, momentum continuation, VWAP reclaim, or RSI reversal — it asks the LLM to score the setup quality (1–10) and estimate entry, target, and stop prices. Setups scoring above `min_publish_quality` (default 5) are published to `scalpbot:setups`.

**The Actuary** `src/agents/risk_modeling_agent.py`
The risk gate. Every setup from the Chartist must pass nine checks before it can proceed:
1. Session is open (within configured trading hours)
2. Setup quality meets minimum threshold (default 6)
3. R/R ratio ≥ minimum (default 2.0)
4. Portfolio heat under limit (default 4% of account)
5. Per-position heat under limit (default 2% of account)
6. No existing open position in that ticker
7. Ticker not on cooldown
8. VIX below halt level (default 35)
9. Daily drawdown halt not triggered

If all nine pass, it sizes the position using ATR-based heat calculation and publishes an approval to `scalpbot:risk-decisions`. If any check fails, the setup is vetoed with a reason logged.

---

### Tier 3 — Decision

**The General** `src/coordinator/trade_coordinator.py`
The only agent that decides whether to actually trade. It listens to risk approvals and scores each one using a weighted conviction formula:

| Component | Weight |
|-----------|--------|
| Setup quality (from Chartist) | 40% |
| Catalyst score (from Wire) | 30% |
| Historical setup weight (from Historian) | 20% |
| Time-of-day bonus (morning open / power hour) | 10% |

If conviction exceeds the threshold (default 5.5 during prime hours, 7.0 off-hours), it emits an ENTER directive to `scalpbot:directives`. Enforces a circuit breaker of 20 directives per session and a 5-minute cooldown per ticker.

---

### Tier 4 — Execution

**The Sniper** `src/execution/order_execution_agent.py`
Receives ENTER directives and places orders via Alpaca. Tries a limit order first; if it isn't filled within 500ms, it converts to market. Orders above $10,000 notional are time-sliced into 3 child orders 0.7 seconds apart. Publishes fill confirmations to `scalpbot:fill-reports`.

**The Watcher** `src/execution/position_monitor_agent.py`
Monitors every open position in real time (1-second poll). Manages three exit conditions:
- **Hard stop**: price hits the stop defined at entry
- **Trailing stop**: activates once the position is 50% of the way to target; trails by 1.5× ATR
- **Time exit**: force-closes any position held longer than 15 minutes

Publishes exit instructions to `scalpbot:exit-orders`.

---

### Tier 5 — Memory

**The Historian** `src/agents/performance_agent.py`
Listens to fill reports and exit orders. When a trade completes (entry matched to exit), it calculates P&L, records the trade to `data/trades.db` (SQLite), updates the running daily P&L in Redis, and recalculates a Kelly-inspired performance weight for that setup type. Weights are fed back to The General so future conviction scores reflect actual historical edge. Flags degrading setups if rolling win rate drops more than 15 percentage points below the all-time win rate.

---

## Configuration

All tunable parameters live in `config/`. Restart agents after changing any of these.

### `config/risk-params.yaml` — Position sizing and safety limits
```yaml
daily_drawdown_halt_pct: 3.0    # Kill session if account drops 3%
max_position_heat_pct: 2.0      # Max % of account risked per trade
max_portfolio_heat_pct: 4.0     # Max combined risk across all open positions
min_rr_ratio: 2.0               # Minimum reward-to-risk to approve a setup
atr_stop_multiplier: 1.5        # Initial stop = 1.5 × ATR below entry
vix_halt_level: 35.0            # Halt all trading if VIX exceeds this
```

### `config/exit-rules.yaml` — How positions are managed
```yaml
trailing_stop_activation_pct: 0.50   # Activate trailing stop at 50% of way to target
trailing_stop_atr_multiplier: 1.5    # Trailing stop trails by 1.5 × ATR
time_exit_minutes: 15                # Force-close any position held over 15 minutes
```

### `config/session.yaml` — Trading hours and conviction thresholds
```yaml
session_open: "09:30"
session_close: "16:00"           # Change to 20:00 for after-hours paper testing
conviction_enter_threshold: 5.5  # Lower = more trades; raise to 7.0 for selectivity
max_directives_per_session: 20   # Circuit breaker
same_ticker_cooldown_s: 300      # 5-minute cooldown between trades on same ticker
```

### `config/indicators.yaml` — Watchlist and technical settings
```yaml
fallback_watchlist:
  - IREN, INTC, CRWV, SOXL, TQQQ, SQQQ, SMCI, F, UVXY, MU, PLTR, LABU
ema_periods: [5, 9, 21]
rsi_period: 14
atr_period: 14
```

### `config/execution.yaml` — Order execution behavior
```yaml
entry_order_type: limit          # "limit" tries limit first, falls back to market
limit_fill_timeout_ms: 500       # Wait 500ms for limit fill before going market
large_order_notional: 10000      # Orders above $10k are sliced
```

---

## Controlling the System

### Dashboard (`http://127.0.0.1:5000`)
- **Launch Agents** — starts all 8 agents as a background process
- **Stop Agents** — graceful shutdown
- **E-STOP** — kills everything immediately, including the Flask server
- **Agent Models panel** — paste any OpenRouter model ID into a field; it saves automatically on input. Each agent can run a different model.

### Live model IDs that work well
| Use case | Model ID |
|----------|----------|
| Fast, cheap (Chartist, Actuary) | `x-ai/grok-4.1-fast` |
| Reasoning-heavy (General) | `x-ai/grok-4.20-multi-agent-beta` |
| Best overall | `anthropic/claude-sonnet-4.6` |
| Budget | `google/gemini-2.0-flash-001` |

### `.env` — Secrets and account config
```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets   ← change to live when ready
OPENROUTER_API_KEY=...
ACCOUNT_VALUE=25000                                 ← used for position sizing
```

---

## Viewing Trade Data After a Session

### 1. Live dashboard
The dashboard at `http://127.0.0.1:5000` shows:
- Daily P&L in the top bar (updates every 2 seconds)
- Open positions panel (bottom left)
- Per-agent activity feed (each panel)

### 2. SQLite database — `data/trades.db`
Every completed trade is written here permanently. The file survives restarts.

**Open with DB Browser for SQLite** (free GUI — [sqlitebrowser.org](https://sqlitebrowser.org)) and browse or export to CSV.

**Or query from terminal:**
```bash
# Last 20 trades
sqlite3 data/trades.db "SELECT ticker, direction, entry_price, exit_price, shares, pnl, exit_reason, recorded_at FROM trades ORDER BY recorded_at DESC LIMIT 20;"

# Today's P&L
sqlite3 data/trades.db "SELECT ROUND(SUM(pnl),2) as total_pnl, COUNT(*) as trades FROM trades WHERE DATE(recorded_at) = DATE('now');"

# Win rate and expectancy by setup type
sqlite3 data/trades.db "
  SELECT
    setup_type,
    COUNT(*) as n,
    ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) * 100, 1) as win_pct,
    ROUND(AVG(pnl), 4) as avg_pnl,
    ROUND(SUM(pnl), 2) as total_pnl
  FROM trades
  GROUP BY setup_type
  ORDER BY total_pnl DESC;
"

# Biggest wins and losses
sqlite3 data/trades.db "SELECT ticker, pnl, exit_reason, recorded_at FROM trades ORDER BY pnl DESC LIMIT 5;"
sqlite3 data/trades.db "SELECT ticker, pnl, exit_reason, recorded_at FROM trades ORDER BY pnl ASC LIMIT 5;"
```

### 3. Redis — live session state
Redis holds the in-memory session state (positions, daily P&L, agent status). This resets when Redis restarts, so `trades.db` is the permanent record.

```bash
# Connect
docker exec -it scalpbot-redis redis-cli

# Useful keys
GET scalpbot:daily-pnl
HGETALL scalpbot:positions
HGETALL scalpbot:risk-params
HGETALL scalpbot:models
```

Or install **RedisInsight** (free GUI) to browse all keys visually.

### 4. Agent logs — `logs/agents.log`
Structured JSON logs from all 8 agents. Use the Log Viewer button in the dashboard, or tail directly:
```bash
tail -f logs/agents.log
```

---

## Message Bus — Redis Pub/Sub Channels

| Channel | Publisher | Subscribers |
|---------|-----------|-------------|
| `scalpbot:market-data` | Scanner | Chartist |
| `scalpbot:catalysts` | Wire | General |
| `scalpbot:setups` | Chartist | Actuary |
| `scalpbot:risk-decisions` | Actuary | General |
| `scalpbot:directives` | General | Sniper |
| `scalpbot:fill-reports` | Sniper | Historian |
| `scalpbot:exit-orders` | Watcher | Historian |
| `scalpbot:performance-updates` | Historian | General |

---

## Requirements

- Python 3.11+
- Docker Desktop (for Redis)
- Alpaca account (paper or live) — [app.alpaca.markets](https://app.alpaca.markets)
- OpenRouter account — [openrouter.ai](https://openrouter.ai)

Python packages are installed automatically by `start.bat`. Key dependencies: `alpaca-py`, `redis`, `flask`, `flask-socketio`, `openai` (OpenRouter-compatible), `pandas`, `pandas-ta`, `structlog`, `pyyaml`, `python-dotenv`.

---

## Disclaimer

This is a paper-trading research tool. Past performance in simulation does not predict live results. Always test thoroughly before trading real capital. The PDT rule requires a $25,000 minimum account balance for more than 3 day trades in 5 business days.
