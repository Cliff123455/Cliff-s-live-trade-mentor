# CliffClaw ScalpBot — Status & Wishlist

*Last updated: March 26, 2026*

---

## What Exists Now

### 8-Agent Architecture (Tiered Pipeline)

- **Scanner (T1)** — Dynamic screener via Alpaca most-actives API. Falls back to a static watchlist in `config/indicators.yaml`. Now merges screener results with fallback list when screener finds fewer than 5 symbols. Supports user-added tickers via Redis (`state:watchlist`).
- **Wire (T1)** — Polls Alpaca News, Truth Social, and X (Twitter) for catalysts. Makes LLM calls via OpenRouter. Disabled in backtest mode (no historical news replay yet).
- **Chartist (T2)** — Technical analysis on 1-min bars. Detects setups: `vwap_reclaim`, `ema_bounce`, `hod_breakout`, `momentum_continuation`, `tape_sweep_follow`, `float_rotation`. Publishes scored setups with `setup_type`.
- **Actuary (T2)** — Risk gating: quality score, duplicate check, position sizing (ATR-based), heat limits, R/R ratio. Now passes `setup_type` through approved and vetoed decisions.
- **General (T3)** — Conviction scoring and trade decision. Makes LLM calls for borderline decisions. Publishes ENTER/PASS/WAIT directives. `setup_type` included in all directives.
- **Sniper (T4)** — Executes via Alpaca: limit order with market fallback. Sliced orders for large notionals. `setup_type` now flows through all fill reports.
- **Watcher (T4)** — Position monitor: trailing stops, hard stops, time exits, target exits. Exit orders now include `setup_type`, `direction`, and `entry_price`.
- **Historian (T5)** — Logs trades to SQLite (`data/trades.db`), computes per-setup performance weights, publishes degradation flags. `setup_type` is now properly stored from fill reports.

### Backtest Mode

- Mode toggle in UI (LIVE / BACKTEST)
- Date picker and speed selector (1x, 5x, 10x, 50x, 100x, MAX)
- **ReplayScanner** replays historical 1-min bars from Alpaca
- **PaperSniper** simulates fills with configurable slippage
- Simulated clock (`state:sim-clock`) — all agents read sim time transparently via `BaseAgent.now_ms()` and `BaseAgent.now_et()`
- General uses sim clock for session-hours gating (fixed from wall-clock bug)
- P&L and positions read from Redis in backtest mode (not Alpaca)

### Dashboard (Flask + SocketIO)

- Compact row for support agents (Scanner, Wire, Watcher, Historian)
- Large 2x2 grid for decision pipeline (Chartist, Actuary, General, Sniper)
- Real-time log streaming via WebSocket
- Rich trade detail formatting in all agent panes (entry/stop/target/R:R/heat/P&L)
- `setup_type` shown in purple across all agent panes
- Live positions table (Ticker, Dir, Shares, Entry, Current, Notional, Stop, Target, P&L)
- LIQUIDATE ALL button with two-click safety
- AI Mentor chat (OpenRouter, reads all Redis state)
- Sidebar: Mode selector, Account balance, Trade Rules, Watchlist manager, Agent Models, Positions
- Watchlist manager: add/remove tickers on the fly, shows config-based (grey) and user-added (gold) tags

### Watchlist Management

- `config/indicators.yaml` has a `fallback_watchlist` (12 tickers)
- Screener-found tickers are merged with fallback (no more replacing when < 5)
- User can add tickers via the UI sidebar — stored in Redis `state:watchlist`
- Scanner picks up user-added tickers on its next refresh cycle
- API: `GET/POST/DELETE /api/watchlist`

### Setup Type Tracking

- `setup_type` now flows through the entire pipeline: Chartist → Actuary → General → Sniper → Watcher → Historian
- Stored in `trades.db` for every completed trade
- Visible in UI log entries for all channels
- Historian computes per-setup-type win rate, expectancy, and degradation flags

### Market Pulse Agent (NEW — Tier 0)

- **The Pulse** — reads SPY, QQQ, VIX-proxy (UVXY) prices from market data stream
- Computes market regime: `BULLISH`, `BEARISH`, `CHOPPY`, `RISK_OFF`
- Computes directional bias: `LONG_PREFERRED`, `SHORT_PREFERRED`, `NEUTRAL`, `NO_NEW_LONGS`
- Dynamic conviction floors adjust per-regime (e.g., longs need 10+ in RISK_OFF = effectively blocked)
- VIX regime levels: normal (<18), moderate (18-25), elevated (25-35), extreme (>35)
- Publishes to `state:market-regime` (Redis) and `scalpbot:market-regime` (pub/sub)
- Pure Python — no LLM calls. HMM-based upgrade planned (separate workstream).
- Runs in both LIVE and BACKTEST modes

### Hard Guardrails (NEW)

- **Max open positions**: 4 concurrent (configurable in `risk-params.yaml` or via UI trade rules)
- **Max total notional**: 60% of account value (prevents 6x $20k positions on $100k account)
- **Market regime gating**: RISK_OFF blocks ALL new positions at the Actuary level; BEARISH blocks longs unless quality >= 8
- **End-of-day force liquidation**: Watcher closes all open positions at 3:45 PM ET — no more overnight positions
- **Regime-adjusted conviction**: General applies 25% conviction penalty to contra-trend trades, 50% in RISK_OFF
- **Dynamic threshold floors**: Pulse publishes per-direction conviction floors that the General respects

---

## Wishlist / Nice-to-Haves

### High Priority

- [ ] **Equity curve HTML report at end of backtest** — The backtest project (`CliffClaw-Backtest/backtest_runner.py`) has `_generate_report_html()` with Chart.js. Needs to be wired into the main project's backtest completion flow.
- [ ] **Per-setup performance dashboard** — Use the `setup_type` data in `trades.db` to show a breakdown: which setups are profitable, which are losing, sample sizes, win rates. Could be a separate page or a section in the sidebar.

### Medium Priority

- [ ] **Candlestick chart page** — TradingView-style charts using the `lightweight-charts` library. Show called setups with markers (entry, stop, target arrows). Possibly a separate page (`/chart?ticker=AAPL`).
- [ ] **Wire in backtest mode** — Would need a historical news replay system. Could use Alpaca historical news API to replay catalysts at the correct sim time.
- [ ] **Screener criteria in UI** — Let Cliff change min_price, max_price, min_trade_count, top_n from the sidebar without editing YAML.
- [ ] **Yahoo Finance integration** — Pull today's top volume movers to supplement the Alpaca screener. Could run as a background poll and merge results.

### Lower Priority / Exploration

- [ ] **Backtest parameter sweep** — The separate `CliffClaw-Backtest` project can test every configuration. Could be wired into the main UI as a batch runner.
- [ ] **Trade replay viewer** — Click on a completed trade in the Historian pane to see the full journey: setup detection → risk approval → conviction → fill → monitoring → exit.
- [ ] **Multi-day backtest** — Currently backtests a single day. Running across date ranges would require looping the replay scanner.
- [ ] **Alert system** — Desktop notifications or sound alerts when a trade is entered or exited.
- [ ] **Session stats panel** — Running summary during a session: total trades, win rate, avg hold time, best/worst trade.
- [ ] **Export trades to CSV** — One-click export from the Historian for analysis in Excel/Google Sheets.
- [ ] **Model cost tracker** — Track OpenRouter token usage per agent per session. Show in the sidebar.

---

## Config File Locations

| File | Purpose |
|------|---------|
| `config/indicators.yaml` | Chartist indicators, screener params, fallback watchlist |
| `config/risk-params.yaml` | Actuary risk gates, sizing, drawdown halt |
| `config/exit-rules.yaml` | Watcher trailing stops, time exits, target exits |
| `config/execution.yaml` | Sniper order types, timeouts, slicing |
| `config/performance.yaml` | Historian min samples, degradation threshold |
| `config/session.yaml` | General conviction thresholds, session hours |

---

## Key Redis State Keys

| Key | Type | Purpose |
|-----|------|---------|
| `state:positions` | Hash | Open positions (ticker → JSON) |
| `state:daily-pnl` | String | Running daily P&L |
| `state:trade-rules` | Hash | UI-overridable trade parameters |
| `state:models` | Hash | OpenRouter model IDs per agent |
| `state:watchlist` | String (JSON list) | User-added tickers |
| `state:mode` | String | "live" or "backtest" |
| `state:sim-clock` | String | Simulated epoch ms (backtest only) |
| `state:setup-weights` | Hash | Per-setup performance weights |
| `state:risk-params` | Hash | Halt flag, risk parameters |
