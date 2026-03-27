# src/shared/constants.py
# Single source of truth for all channel names and state store keys.
# If you rename anything here, update AGENTS.md on the same branch.

# ── Redis pub/sub channels ────────────────────────────────────────────────────
class CH:
    MARKET_DATA         = "scalpbot:market-data"
    CATALYSTS           = "scalpbot:catalysts"
    SETUPS              = "scalpbot:setups"
    RISK_DECISIONS      = "scalpbot:risk-decisions"
    DIRECTIVES          = "scalpbot:directives"
    FILL_REPORTS        = "scalpbot:fill-reports"
    EXIT_ORDERS         = "scalpbot:exit-orders"
    PERFORMANCE_UPDATES = "scalpbot:performance-updates"
    DAILY_HALT          = "scalpbot:risk:daily-halt"
    MARKET_REGIME       = "scalpbot:market-regime"
    MARKOV_PREDICTION   = "scalpbot:markov-prediction"


# ── Shared state store keys ───────────────────────────────────────────────────
class SK:
    MARKET          = "state:market:{}"          # .format(ticker)
    CATALYST        = "state:catalyst:{}"        # .format(ticker)
    SETUPS          = "state:setups"
    RISK_PARAMS     = "state:risk-params"
    POSITIONS       = "state:positions"
    PENDING_ORDERS  = "state:pending-orders"
    DAILY_PNL       = "state:daily-pnl"
    TRADE_LOG       = "state:trade-log"          # Redis stream
    SETUP_WEIGHTS   = "state:setup-weights"
    AGENT_STATUS    = "state:agent-status:{}"    # .format(agent_name)
    MODELS          = "state:models"             # OpenRouter model IDs per agent
    WATCHLIST       = "state:watchlist"          # JSON list of user-added tickers
    MARKET_REGIME   = "state:market-regime"     # JSON regime object from The Pulse
    MARKOV_STATE    = "state:markov"           # JSON Markov prediction from The Oracle


# ── Agent names ───────────────────────────────────────────────────────────────
class AGENTS:
    SCANNER    = "scanner"
    WIRE       = "wire"
    CHARTIST   = "chartist"
    ACTUARY    = "actuary"
    GENERAL    = "general"
    SNIPER     = "sniper"
    WATCHER    = "watcher"
    HISTORIAN  = "historian"
    PULSE      = "pulse"
    ORACLE     = "oracle"

    ALL = [SCANNER, WIRE, CHARTIST, ACTUARY, GENERAL, SNIPER, WATCHER, HISTORIAN, PULSE, ORACLE]


# ── Default OpenRouter model IDs per agent ────────────────────────────────────
DEFAULT_MODELS = {
    AGENTS.SCANNER:   "google/gemini-2.0-flash-001",
    AGENTS.WIRE:      "google/gemini-2.0-flash-001",
    AGENTS.CHARTIST:  "anthropic/claude-3.5-sonnet",
    AGENTS.ACTUARY:   "anthropic/claude-3.5-sonnet",
    AGENTS.GENERAL:   "anthropic/claude-opus-4-5",
    AGENTS.SNIPER:    "google/gemini-2.0-flash-001",
    AGENTS.WATCHER:   "google/gemini-2.0-flash-001",
    AGENTS.HISTORIAN: "anthropic/claude-haiku-4-5-20251001",
    AGENTS.PULSE:     "google/gemini-2.0-flash-001",  # No LLM calls — placeholder only
    AGENTS.ORACLE:    "google/gemini-2.0-flash-001",  # No LLM calls — pure math
}


# ── Setup types ───────────────────────────────────────────────────────────────
class SETUP:
    VWAP_RECLAIM          = "vwap_reclaim"
    EMA_BOUNCE            = "ema_bounce"
    HOD_BREAKOUT          = "hod_breakout"
    MOMENTUM_CONTINUATION = "momentum_continuation"
    TAPE_SWEEP_FOLLOW     = "tape_sweep_follow"
    FLOAT_ROTATION        = "float_rotation"


# ── Catalyst types ────────────────────────────────────────────────────────────
class CATALYST:
    EARNINGS_BEAT  = "earnings_beat"
    EARNINGS_MISS  = "earnings_miss"
    UPGRADE        = "upgrade"
    DOWNGRADE      = "downgrade"
    FDA_APPROVAL   = "fda_approval"
    FDA_REJECTION  = "fda_rejection"
    MACRO_PRINT    = "macro_print"
    SOCIAL_SPIKE   = "social_spike"
    SEC_FILING     = "sec_filing"
    NONE           = "none"
