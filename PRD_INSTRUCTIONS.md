# PRD_INSTRUCTIONS.md — Instructions for Antigravity
#
# Cliff: Paste your project ID into Antigravity and say:
#   "Read PRD_INSTRUCTIONS.md and execute it."
# That's it. Antigravity will handle the rest.

---

# Task: Generate a Product Requirements Document (PRD) for CliffClaw v2.0

You are working on the **CliffClaw / ScalpBot** project — an autonomous intraday
scalping system built on a multi-agent architecture with Redis pub/sub, Flask
dashboard, Alpaca paper trading, and OpenRouter LLM integration.

**Your job:** Analyze every version of this codebase from the last two weeks,
identify what works and what doesn't, then produce a comprehensive PRD for the
next version. The PRD must be saved to `docs/PRD_v2.0.md`.

---

## Step 1: Audit Every Version (What Exists)

Read these files to understand the full system. Do NOT skip any.

### Architecture & Contracts
- `AGENTS.md` — Agent communication contracts, payload schemas, channel directory
- `AGENT_README.md` — Per-agent reference with config, model, and role details
- `STATUS.md` — Current feature status and wishlist
- `README.md` — Quick start, architecture overview, config reference
- `.antigravity/rules.md` — Project constitution, branch strategy, collaboration rules
- `MENTOR_FEATURE_BRIEF.md` — AI Mentor feature specification

### Agent Source Code (read every file)
- `src/shared/base_agent.py` — BaseAgent abstract class (Redis, pub/sub, LLM, heartbeat)
- `src/shared/constants.py` — Channel names (CH), state keys (SK), agent names (AGENTS)
- `src/shared/sim_clock.py` — Simulated clock for backtest mode
- `src/agents/market_data_agent.py` — The Scanner (Tier 1) — live quote streaming
- `src/agents/news_sentiment_agent.py` — The Wire (Tier 1) — news + social sentiment
- `src/agents/technical_analysis_agent.py` — The Chartist (Tier 2) — setup detection
- `src/agents/risk_modeling_agent.py` — The Actuary (Tier 2) — risk veto + sizing
- `src/agents/market_pulse_agent.py` — The Pulse (Tier 0) — rule-based market regime
- `src/agents/markov_regime_agent.py` — The Oracle (Tier 0) — Markov chain predictor (NEW)
- `src/agents/performance_agent.py` — The Historian (Tier 5) — trade logging + weights
- `src/coordinator/trade_coordinator.py` — The General (Tier 3) — conviction + directives
- `src/execution/order_execution_agent.py` — The Sniper (Tier 4) — order execution
- `src/execution/position_monitor_agent.py` — The Watcher (Tier 4) — exit management

### Backtest Infrastructure
- `src/backtest/replay_scanner.py` — ReplayScanner: historical bar replay
- `src/backtest/paper_sniper.py` — PaperSniper: simulated fills with slippage
- `backtest_runner.py` — Standalone backtest orchestrator with HTML reports
- `batch_backtest.py` — 27-configuration batch runner for Markov model testing
- `agents_runner.py` — CLI launcher for both LIVE and BACKTEST modes

### Configuration (read every YAML file)
- `config/indicators.yaml` — Chartist indicators, screener params, watchlist
- `config/risk-params.yaml` — Risk gates, position sizing, drawdown halt
- `config/session.yaml` — Trading hours, conviction thresholds, weights
- `config/execution.yaml` — Order routing, limit timeouts, slicing
- `config/exit-rules.yaml` — Trailing stops, time exits, target exits
- `config/performance.yaml` — Historian sample sizes, degradation thresholds
- `config/backtest_matrix.yaml` — 27-config Markov parameter grid (NEW)

### UI & Dashboard
- `ui/app.py` — Flask backend: all routes, WebSocket, Alpaca integration
- `ui/mentor.py` — AI Mentor chat endpoint (OpenRouter)
- `ui/templates/index.html` — Full dashboard HTML/CSS/JS (the "3 AM version")

### Database
- `data/trades.db` — SQLite with tables: `trades`, `backtest_runs`, `batch_runs`

### Git History
- Run `git log --all --oneline --since="2 weeks ago"` to see all commits
- Run `git branch -a` to see all branches
- Read the diff between the first commit and HEAD to understand the evolution

---

## Step 2: Evaluate What Works vs. What Doesn't

### KEEP (proven good — carry forward to v2.0)

Score each of these on a 1-10 scale based on code quality, design, and completeness:

1. **10-Agent Tiered Architecture** — The 5-tier pipeline (Pulse → Sensors → Analysts → Decision → Execution → Memory) with The Oracle added as Tier 0 Markov predictor
2. **Redis Message Bus** — Pub/sub channels with JSON payloads, shared state store
3. **BaseAgent Pattern** — Abstract base class with Redis, pub/sub, LLM, heartbeat, sim clock
4. **Risk Guardrails** — 9 veto conditions, daily halt, max positions, max notional, regime gating
5. **Conviction Scoring** — Weighted formula with regime adjustment and Markov adjustment
6. **Backtest Infrastructure** — ReplayScanner + PaperSniper + sim clock + identical message formats
7. **Batch Backtesting** — 27-config parameter sweep with model rotation
8. **SQLite Trade Journal** — Permanent record with backtest_runs and batch_runs tables
9. **AI Mentor** — Read-only conversational AI with full Redis context
10. **Dashboard HTML** (the 3 AM version in `ui/templates/index.html`) — Dark theme, agent panes, positions table, WebSocket streaming, model selector, watchlist manager

### REMOVE or REWORK (problems found)

Identify issues in each category:

1. **Configuration drift** — Config values are split between YAML files, Redis state, UI overrides, and .env. No single source of truth at runtime.
2. **No exportable configuration** — Cannot save/load/share a complete config snapshot that reproduces a trading session. The batch_backtest.py partially addresses this but only for Markov params.
3. **Backtest-only features** — The Pulse and Oracle exist but their predictions aren't visible in the dashboard during live trading.
4. **Missing DB queries** — The extensive trades.db has data but no API endpoints or UI pages to query it (win rate by setup type, P&L by day, best/worst trades, etc.).
5. **Wire agent limitations** — Disabled in backtest mode, no historical news replay.
6. **No multi-day backtest** — Can only replay one day at a time.
7. **No comparison view** — After running batch backtests, there's no dashboard to compare results visually.
8. **Model cost tracking** — No visibility into OpenRouter token usage per agent.

---

## Step 3: Write the PRD

Create `docs/PRD_v2.0.md` with this exact structure:

```markdown
# CliffClaw v2.0 — Product Requirements Document

## 1. Executive Summary
[2-3 paragraphs: what CliffClaw is, what v2.0 adds, why it matters]

## 2. System Architecture (Carried Forward)
[Diagram of the 10-agent, 6-tier pipeline including Oracle]
[List every agent, its tier, file, channel, and role]
[List every Redis channel and state key]

## 3. What's New in v2.0

### 3.1 Exportable Configuration System
- A single JSON/YAML file that captures the COMPLETE runtime state:
  - All 6 YAML config files merged
  - Active OpenRouter model IDs per agent
  - Conviction threshold overrides
  - Markov parameters (decay, window, thresholds)
  - Watchlist (config + user-added)
  - Risk parameter overrides
- Import/export via UI button and CLI flag
- `--config <path>` flag on agents_runner.py and backtest_runner.py
- Every backtest run automatically snapshots its config (already partially done in backtest_runs.config_snapshot)
- API endpoints: GET/POST /api/config/export, /api/config/import

### 3.2 Database Query Dashboard
- New UI page: `/analytics` or sidebar section
- Pre-built queries with visual output:
  - P&L by day (bar chart)
  - Win rate by setup_type (table + chart)
  - Best/worst trades (sortable table)
  - Performance over time (equity curve from trades.db)
  - Batch backtest comparison (table: config vs P&L vs win rate)
  - Model performance comparison (which General model performed best)
- Raw SQL query input for power users
- Export to CSV button
- API endpoints: GET /api/analytics/daily-pnl, /api/analytics/setup-performance, /api/analytics/batch-results

### 3.3 Updated Dashboard (from 3 AM version)
- Carry forward the EXACT HTML/CSS/JS from ui/templates/index.html
- Add these new panels:
  - **Oracle Panel** — Shows current Markov state, predicted next state, transition probabilities, conviction adjustment. Updates every 30s.
  - **Pulse Panel** — Shows regime (BULLISH/BEARISH/CHOPPY/RISK_OFF), SPY/QQQ change %, VIX level, conviction floors. Already exists as a data source but needs its own visible panel.
  - **Config Export/Import** buttons in the sidebar
  - **Analytics** link in the sidebar to the query dashboard
  - **Batch Results** viewer: after running batch_backtest.py, see a summary table in the UI
- Preserve all existing panels: Scanner, Wire, Chartist, Actuary, General, Sniper, Watcher, Historian, Positions, Mentor, Model Selector, Watchlist Manager, Trade Rules

### 3.4 Markov Model Integration (Carried Forward from this session)
- The Oracle agent (src/agents/markov_regime_agent.py) — already built
- 10 composite states: return direction × volatility
- Online transition matrix with exponential decay
- Conviction adjustment (±0.20 max, scaled by confidence)
- Integration with The General's scoring
- Configurable via backtest_matrix.yaml and runtime Redis overrides

### 3.5 Batch Backtesting with Model Rotation (Carried Forward)
- batch_backtest.py — already built
- 27 configurations: 3 conviction thresholds × 3 decay factors × 3 window sizes
- General model rotates through top 5 finance models
- All other agents use base model (ChatGPT-4o)
- Results logged to batch_runs table in trades.db
- CSV export

## 4. Database Schema (Complete)

### 4.1 trades table (existing)
[Document all columns: id, directive_id, setup_id, ticker, setup_type, direction, entry_price, exit_price, shares, entry_ts, exit_ts, pnl, exit_reason, recorded_at]

### 4.2 backtest_runs table (existing)
[Document all columns from backtest_runner.py _ensure_runs_table()]

### 4.3 batch_runs table (existing)
[Document all columns from batch_backtest.py _ensure_batch_table()]

### 4.4 NEW: config_snapshots table
- id, snapshot_name, created_at, config_json, notes
- Links to backtest_runs and batch_runs via foreign key

## 5. Configuration File Reference

For each config file, document:
- File path
- Owner agent
- Every parameter with type, default value, valid range, and description
- Which parameters are overridable from the UI at runtime

Files:
- config/indicators.yaml
- config/risk-params.yaml
- config/session.yaml
- config/execution.yaml
- config/exit-rules.yaml
- config/performance.yaml
- config/backtest_matrix.yaml

## 6. API Endpoints (Complete)

Document every existing and new endpoint:
- Method, path, request body, response body, description
- Group by: Dashboard, Trading, Config, Analytics, Mentor, WebSocket

## 7. Redis State & Channel Directory

### 7.1 Pub/Sub Channels
[Full table: channel name, publisher, subscribers, payload schema reference]
Include new: scalpbot:markov-prediction

### 7.2 State Store Keys
[Full table: key, type, writer, description]
Include new: state:markov

## 8. Agent Specifications

For each of the 10 agents, document:
- Name, codename, tier, file path
- Role (1 sentence)
- Subscribes to (channels)
- Publishes to (channels)
- State reads / writes
- Config file
- OpenRouter model (default)
- Key algorithms or formulas
- Circuit breakers / safety limits

Agents: Pulse, Oracle, Scanner, Wire, Chartist, Actuary, General, Sniper, Watcher, Historian

## 9. Backtest System

### 9.1 Single-day backtest (backtest_runner.py)
### 9.2 Batch backtest (batch_backtest.py)
### 9.3 Agent substitutions (ReplayScanner, PaperSniper)
### 9.4 Sim clock system
### 9.5 Report generation (HTML + CSV)

## 10. Risk Management Matrix

[Table of all 9+ veto conditions with thresholds, which agent enforces them, whether they're configurable, and override policy]

## 11. Non-Functional Requirements

- Latency: agent loop < 100ms, LLM calls < 5s
- Reliability: agents crash independently, auto-restart via runner
- Data persistence: trades.db survives Redis restart
- Security: no API keys in git, paper mode by default
- Observability: structured JSON logs, Redis state inspection, dashboard

## 12. Migration Path from v1.x

[Step-by-step instructions for upgrading an existing installation]
- New dependencies
- New config files
- New Redis keys to initialize
- Database migrations (new tables)
- New agents to register

## 13. Open Questions / Future Work

[Pull from STATUS.md wishlist + anything identified during audit]
```

---

## Step 4: Validate

After generating the PRD:

1. Cross-reference every agent file against the PRD's agent specs — no agent should be missing
2. Cross-reference every config parameter against the PRD's config reference — no param should be undocumented
3. Cross-reference every Redis channel and state key against the PRD's directory — no key should be missing
4. Verify the database schema matches what's actually in the code
5. Verify all API endpoints in ui/app.py are documented

---

## Step 5: Output

Save the completed PRD to: `docs/PRD_v2.0.md`

Then print a summary:
- How many agents documented
- How many config parameters documented
- How many API endpoints documented
- How many Redis channels/keys documented
- Top 3 highest-priority new features identified
- Top 3 issues flagged for removal/rework

---

## Important Rules

1. **Do NOT modify any existing code.** This task is documentation only.
2. **Do NOT invent features that don't exist.** Document what IS built and what SHOULD be built.
3. **Be specific.** Include file paths, line numbers, exact parameter names, actual default values.
4. **Use the actual code as truth**, not just the docs (docs may be outdated).
5. **The database is extensive** — query it if possible to understand what data exists. Use the schema from the code, not assumptions.
6. **The 3 AM HTML version** (`ui/templates/index.html`) is the golden UI — the PRD should carry it forward with additions, not replace it.
