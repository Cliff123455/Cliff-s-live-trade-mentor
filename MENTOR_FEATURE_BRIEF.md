# Feature Brief: Add AI Mentor Chat to CliffClaw

> **Hand this file to any Claude session working on the CliffClaw project.**
> It describes a new feature to add based on what we built in the Day Trading Mentor project.

---

## What We Want

Add an **AI Mentor panel** to CliffClaw's live trading dashboard. The Mentor is:

1. **A conversational AI** that Cliff can talk to during live trading sessions
2. **An observer** that watches all the agents (Sniper, Historian, Risk, etc.) and can explain what they're doing and why
3. **A teacher** that can explain patterns, setups, and market conditions in plain English
4. **A data analyst** that can pull up real-time and historical data on demand

Think of it as a senior trader sitting next to Cliff, watching the same screens, answering questions.

---

## How It Should Work

### Chat Panel in the UI
- Add a chat panel to the CliffClaw dashboard (right side or collapsible)
- Text input at the bottom, conversation history above
- Cliff types a question, gets an answer in 2-5 seconds
- The Mentor has access to all Redis data (candles, quotes, alerts, agent state, trade history)

### Example Conversations

**Cliff:** "Why did the Sniper pass on that NVDA setup?"
**Mentor:** "The Sniper detected a bullish engulfing on NVDA at $178.50, but the Risk Agent vetoed it because your daily drawdown is at 2.1% (halt threshold is 3%). The conviction score was 6/10 — below your enter threshold of 7."

**Cliff:** "What's the best setup you see right now?"
**Mentor:** "INTC has a volume spike (4.2x 20-bar avg) with a breakout above the 10-bar high of $24.15. Three consecutive green candles with increasing volume. If you were to trade it: entry above $24.20, stop at $23.95 (below the breakout candle), target $24.60 (measured move)."

**Cliff:** "How did we do today?"
**Mentor:** "3 trades today. Won 2, lost 1. Net P&L: +$147. Best trade: TSLA long at $248.30, exited at $249.10 (+$80). The loss was SQQQ — entered on a false breakdown signal, stopped out for -$45. Overall win rate this week: 62%."

**Cliff:** "What's a bear flag?"
**Mentor:** "A bear flag is a continuation pattern. After a sharp move down (the pole), price consolidates in a tight upward channel (the flag). The breakdown below the flag's support continues the downtrend. On TradingView, you can spot these by looking for: (1) a sharp red candle or series of red candles, (2) followed by 3-5 small candles drifting up on declining volume, (3) the breakout happens when price drops below the flag's lower trendline with volume."

### Model Selector
- Add a text input where Cliff can paste any OpenRouter model ID
- Saved to Redis key `mentor:model`, persists across restarts
- Default: `anthropic/claude-3.5-sonnet` (fast, good at analysis)
- Cliff can swap to other models to compare their analysis style

---

## Technical Implementation

### What Already Works (copy from Day Trading Mentor)

We built this in the Day Trading Mentor project (`Claude-Day-Trade-mentor/`). The working code is:

**Backend — `/api/chat` endpoint** (see `ui/server.py` lines 120-190):
- Receives `{ message, ticker }` from the frontend
- Reads recent candles from Redis (`mentor:candle-history:{ticker}`)
- Reads current quote from Redis (`mentor:quote:{ticker}`)
- Builds a system prompt with the candle data as context
- Calls OpenRouter API (via `openai` Python SDK with custom base_url)
- Returns `{ reply }` as JSON

**Backend — `/api/model` endpoint** (see `ui/server.py` lines 100-118):
- GET: returns current model from Redis or env default
- POST: saves new model ID to Redis

**Frontend — Chat UI** (see `ui/templates/dashboard.html`):
- Chat messages panel with user/AI message styling
- Text input + send button
- "Thinking..." indicator while waiting for response
- Auto-scroll to newest message

**Frontend — Model selector** (see `ui/templates/dashboard.html`):
- Text input for model ID
- "Active: model-name" status display
- Saves on Enter key

### What to Add for CliffClaw

The CliffClaw Mentor needs **more context** than the Day Trading Mentor version:

1. **Agent state** — Read from Redis keys like `state:trade-rules`, agent tier channels
2. **Trade history** — Read from `trades.db` or Redis trade log
3. **Risk state** — Current drawdown, position heat, portfolio exposure
4. **Session stats** — Win rate, P&L, number of trades today

The system prompt should include all of this so the Mentor can answer questions about what the agents are doing.

### Environment Variables Needed
```
OPENROUTER_API_KEY=sk-or-v1-...    # Already in CliffClaw .env
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
ANALYSIS_MODEL=anthropic/claude-3.5-sonnet  # Default, changeable from UI
```

### Redis Keys
```
mentor:model          — Active model ID (string)
mentor:chat-history   — Optional: store conversation for context window
```

---

## What NOT to Do

- **Don't give the Mentor execution power.** It observes and explains, it doesn't trade.
- **Don't remove existing UI controls.** Add the chat panel alongside what's already there.
- **Don't slow down the main trading loop.** The chat endpoint runs in its own request thread — it should never block candle processing or order execution.

---

## Reference Implementation

The full working code is in:
```
Claude-Day-Trade-mentor/
├── ui/server.py              — /api/chat and /api/model endpoints
├── ui/templates/dashboard.html — Chat UI + Model selector (CSS + JS)
└── src/analyzer.py           — Pattern detection + LLM analysis
```

The Day Trading Mentor project is at:
`C:\Users\Owner\.gemini\antigravity\scratch\0DayTradingHelp\Claude Day-trading-mentor\Claude-Day-Trade-mentor\`

---

*Written 2026-03-26 by Claude (Opus 4.6) during a session with Cliff.*
