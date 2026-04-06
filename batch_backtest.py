# batch_backtest.py — Run 27 Markov configurations through the backtester.
#
# Reads config/backtest_matrix.yaml for the parameter grid, then runs each
# configuration sequentially through the full agent pipeline.  The General
# (manager) model rotates through the top 5 finance models; all other
# agents use the base model (ChatGPT-4o).
#
# Usage:
#   python batch_backtest.py
#   python batch_backtest.py --date 2026-03-24
#   python batch_backtest.py --date 2026-03-24 --tickers AAPL,NVDA,SPY
#   python batch_backtest.py --dry-run          # Print configs without running
#
# Results are logged to data/trades.db (backtest_runs + batch_runs tables)
# and a summary CSV is exported to data/reports/batch_summary_<ts>.csv.

# ── SSL fix (same as backtest_runner.py) ─────────────────────────────────────
import os
os.environ.pop("SSLKEYLOGFILE", None)

import ssl as _ssl
_orig_create = _ssl.create_default_context
def _patched_create(*args, **kwargs):
    ctx = _orig_create(*args, **kwargs)
    try:
        ctx.keylog_filename = None
    except Exception:
        pass
    return ctx
_ssl.create_default_context = _patched_create
# ── End SSL fix ──────────────────────────────────────────────────────────────

import argparse
import asyncio
import csv
import itertools
import json
import logging
import sqlite3
import sys
import time
import traceback
import warnings
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

# Suppress noisy libraries
logging.getLogger("pandas_ta").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", category=FutureWarning, module="pandas_ta")

os.chdir(Path(__file__).resolve().parent)

import redis.asyncio as aioredis

# ── Load matrix config ───────────────────────────────────────────────────────

MATRIX_PATH = Path("config/backtest_matrix.yaml")

def load_matrix() -> dict:
    with open(MATRIX_PATH) as f:
        return yaml.safe_load(f)


def build_configurations(matrix: dict) -> list[dict]:
    """Build all 27 configurations from the parameter grid.

    Each config gets a General model assigned round-robin from the top 5.
    """
    grid = matrix["markov_grid"]
    general_models = matrix["general_models"]
    base_model = matrix["base_model"]

    thresholds = grid["conviction_threshold"]
    decays = grid["decay_factor"]
    windows = grid["window_size"]

    configs = []
    for i, (thresh, decay, window) in enumerate(
        itertools.product(thresholds, decays, windows)
    ):
        general_model = general_models[i % len(general_models)]
        config = {
            "config_id": i + 1,
            "label": f"C{i+1:02d}_t{thresh}_d{decay}_w{window}",
            "conviction_threshold": thresh,
            "markov_decay_factor": decay,
            "markov_window_size": window,
            "general_model": general_model,
            "base_model": base_model,
        }
        configs.append(config)

    return configs


# ── Agent launcher (same pattern as backtest_runner.py) ──────────────────────

async def _start_agent(AgentClass, name: str, **kwargs):
    try:
        agent = AgentClass(**kwargs) if kwargs else AgentClass()
        await agent.start()
    except asyncio.CancelledError:
        pass
    except Exception:
        print(f"    [!] {name} CRASHED: {traceback.format_exc()}", flush=True)


async def _wait_for_replay_done(redis_url: str, tasks: list, grace_s: float):
    r = aioredis.from_url(redis_url, decode_responses=True)
    ps = r.pubsub()
    await ps.subscribe("scalpbot:market-data")
    try:
        async for message in ps.listen():
            if message["type"] != "message":
                continue
            try:
                data = json.loads(message["data"])
            except (json.JSONDecodeError, TypeError):
                continue
            if data.get("ticker") == "__REPLAY_DONE__":
                await asyncio.sleep(grace_s)
                for t in tasks:
                    t.cancel()
                break
    except asyncio.CancelledError:
        pass
    finally:
        await ps.aclose()
        await r.aclose()


async def _clean_state(redis_url: str):
    r = aioredis.from_url(redis_url, decode_responses=True)
    keys = [
        "state:positions", "state:pending-orders", "state:daily-pnl",
        "state:risk-params", "state:setups", "state:setup-weights",
        "state:trade-log", "state:sim-clock", "state:market-regime",
        "state:markov",
    ]
    for key in keys:
        await r.delete(key)
    async for key in r.scan_iter("state:market:*"):
        await r.delete(key)
    async for key in r.scan_iter("state:catalyst:*"):
        await r.delete(key)
    async for key in r.scan_iter("state:agent-status:*"):
        await r.delete(key)
    # Clear model overrides
    await r.delete("state:models")
    await r.delete("state:trade-rules")
    await r.aclose()


def _clear_trades_db():
    db_path = Path("data/trades.db")
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM trades")
    conn.commit()
    conn.close()


# ── Set models in Redis ──────────────────────────────────────────────────────

async def _set_models(redis_url: str, config: dict):
    """Write OpenRouter model IDs to Redis so agents pick them up at runtime."""
    from src.shared.constants import AGENTS

    r = aioredis.from_url(redis_url, decode_responses=True)

    base = config["base_model"]
    general = config["general_model"]

    model_map = {
        AGENTS.SCANNER:   base,
        AGENTS.WIRE:      base,
        AGENTS.CHARTIST:  base,
        AGENTS.ACTUARY:   base,
        AGENTS.GENERAL:   general,   # Only the manager switches models
        AGENTS.SNIPER:    base,
        AGENTS.WATCHER:   base,
        AGENTS.HISTORIAN: base,
        AGENTS.PULSE:     base,
        AGENTS.ORACLE:    base,
    }

    for agent_name, model_id in model_map.items():
        await r.hset("state:models", agent_name, model_id)

    # Set conviction threshold via trade-rules (The General reads this)
    await r.hset("state:trade-rules", "conviction_threshold",
                 str(config["conviction_threshold"]))

    await r.aclose()


# ── Run a single backtest configuration ──────────────────────────────────────

async def run_single_config(
    config: dict,
    date: str,
    speed: float,
    tickers: list[str] | None,
    slippage_bps: float,
    grace_s: float,
    with_wire: bool,
) -> dict:
    """Run one backtest config and return results dict."""
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

    # Clean state
    await _clean_state(redis_url)
    _clear_trades_db()

    # Set models + threshold in Redis before agents start
    await _set_models(redis_url, config)

    # Suppress pandas_ta
    try:
        import pandas_ta
        pandas_ta.verbose = False
    except Exception:
        pass

    # Build agent list
    from src.backtest.replay_scanner import ReplayScanner
    from src.backtest.paper_sniper import PaperSniper
    from src.agents.technical_analysis_agent import TechnicalAnalysisAgent
    from src.agents.risk_modeling_agent import RiskModelingAgent
    from src.coordinator.trade_coordinator import TradeCoordinator
    from src.execution.position_monitor_agent import PositionMonitorAgent
    from src.agents.performance_agent import PerformanceAgent
    from src.agents.market_pulse_agent import MarketPulseAgent
    from src.agents.markov_regime_agent import MarkovRegimeAgent

    agent_specs = [
        (MarketPulseAgent,       "The Pulse",            {}),
        (MarkovRegimeAgent,      "The Oracle",           {
            "decay_factor": config["markov_decay_factor"],
            "window_size": config["markov_window_size"],
        }),
        (ReplayScanner,          "The Scanner (Replay)", {
            "replay_date": date, "speed": speed, "watchlist": tickers,
        }),
        (TechnicalAnalysisAgent, "The Chartist",         {}),
        (RiskModelingAgent,      "The Actuary",          {}),
        (TradeCoordinator,       "The General",          {}),
        (PaperSniper,            "The Sniper (Paper)",   {"slippage_bps": slippage_bps}),
        (PositionMonitorAgent,   "The Watcher",          {}),
        (PerformanceAgent,       "The Historian",        {}),
    ]

    if with_wire:
        from src.agents.news_sentiment_agent import NewsSentimentAgent
        agent_specs.insert(3, (NewsSentimentAgent, "The Wire", {}))

    # Launch
    tasks = [
        asyncio.create_task(_start_agent(cls, name, **kwargs))
        for cls, name, kwargs in agent_specs
    ]
    monitor = asyncio.create_task(
        _wait_for_replay_done(redis_url, tasks, grace_s)
    )

    t0 = time.monotonic()
    try:
        await asyncio.gather(monitor, *tasks, return_exceptions=True)
    except (asyncio.CancelledError, KeyboardInterrupt):
        for t in tasks:
            t.cancel()
        monitor.cancel()
        await asyncio.gather(monitor, *tasks, return_exceptions=True)

    elapsed = time.monotonic() - t0

    # Collect results
    return _collect_results(config, date, elapsed)


def _collect_results(config: dict, date: str, elapsed: float) -> dict:
    """Read trades.db and return a results dict for this run."""
    db_path = Path("data/trades.db")
    if not db_path.exists():
        return {**config, "date": date, "elapsed_s": round(elapsed, 1),
                "trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "total_pnl": 0, "avg_win": 0, "avg_loss": 0, "profit_factor": 0}

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT pnl FROM trades ORDER BY id").fetchall()
    conn.close()

    pnls = [r[0] or 0 for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    win_rate = len(wins) / len(pnls) * 100 if pnls else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    gross_wins = sum(wins) if wins else 0
    gross_losses = abs(sum(losses)) if losses else 0
    pf = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 0

    return {
        **config,
        "date": date,
        "elapsed_s": round(elapsed, 1),
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": pf,
    }


# ── Batch results logging ───────────────────────────────────────────────────

def _ensure_batch_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS batch_runs (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id            TEXT,
            config_id           INTEGER,
            label               TEXT,
            replay_date         TEXT,
            conviction_threshold REAL,
            markov_decay_factor REAL,
            markov_window_size  INTEGER,
            general_model       TEXT,
            base_model          TEXT,
            total_trades        INTEGER,
            wins                INTEGER,
            losses              INTEGER,
            win_rate            REAL,
            total_pnl           REAL,
            avg_win             REAL,
            avg_loss            REAL,
            profit_factor       REAL,
            elapsed_s           REAL,
            run_timestamp       TEXT
        )
    """)
    conn.commit()


def _log_batch_result(batch_id: str, result: dict):
    db_path = Path("data/trades.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    _ensure_batch_table(conn)

    conn.execute(
        "INSERT INTO batch_runs "
        "(batch_id, config_id, label, replay_date, conviction_threshold, "
        "markov_decay_factor, markov_window_size, general_model, base_model, "
        "total_trades, wins, losses, win_rate, total_pnl, avg_win, avg_loss, "
        "profit_factor, elapsed_s, run_timestamp) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            batch_id,
            result["config_id"],
            result["label"],
            result["date"],
            result["conviction_threshold"],
            result["markov_decay_factor"],
            result["markov_window_size"],
            result["general_model"],
            result["base_model"],
            result["trades"],
            result["wins"],
            result["losses"],
            result["win_rate"],
            result["total_pnl"],
            result["avg_win"],
            result["avg_loss"],
            result["profit_factor"],
            result["elapsed_s"],
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def _export_csv(batch_id: str, results: list[dict]) -> str:
    """Export batch results to CSV."""
    reports_dir = Path("data/reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = reports_dir / f"batch_summary_{ts}.csv"

    fieldnames = [
        "config_id", "label", "date", "conviction_threshold",
        "markov_decay_factor", "markov_window_size", "general_model",
        "base_model", "trades", "wins", "losses", "win_rate",
        "total_pnl", "avg_win", "avg_loss", "profit_factor", "elapsed_s",
    ]

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    return str(csv_path)


# ── Print summary ────────────────────────────────────────────────────────────

def _print_summary(results: list[dict]):
    print(f"\n{'='*110}")
    print(f"  BATCH BACKTEST SUMMARY — {len(results)} configurations")
    print(f"{'='*110}")
    print(
        f"  {'#':>3} {'Label':<26} {'Thresh':>6} {'Decay':>5} {'Win':>3} "
        f"{'Model (General)':<42} {'Trades':>6} {'W/L':>6} {'WR%':>6} "
        f"{'P&L':>10} {'PF':>5}"
    )
    print(f"  {'-'*108}")

    for r in results:
        model_short = r["general_model"].split("/")[-1][:40]
        wl = f"{r['wins']}/{r['losses']}"
        print(
            f"  {r['config_id']:>3} {r['label']:<26} "
            f"{r['conviction_threshold']:>6.1f} {r['markov_decay_factor']:>5.2f} "
            f"{r['markov_window_size']:>3} "
            f"{model_short:<42} "
            f"{r['trades']:>6} {wl:>6} {r['win_rate']:>5.1f}% "
            f"${r['total_pnl']:>+9.2f} {r['profit_factor']:>5.2f}"
        )

    # Best / worst
    if results:
        by_pnl = sorted(results, key=lambda r: r["total_pnl"], reverse=True)
        best = by_pnl[0]
        worst = by_pnl[-1]
        print(f"\n  BEST:  {best['label']} — ${best['total_pnl']:+,.2f} "
              f"({best['general_model'].split('/')[-1]}, WR {best['win_rate']:.1f}%)")
        print(f"  WORST: {worst['label']} — ${worst['total_pnl']:+,.2f} "
              f"({worst['general_model'].split('/')[-1]}, WR {worst['win_rate']:.1f}%)")

        # Best model average
        model_pnls: dict[str, list[float]] = {}
        for r in results:
            model_pnls.setdefault(r["general_model"], []).append(r["total_pnl"])
        print(f"\n  MODEL AVERAGES:")
        for model, pnls in sorted(model_pnls.items(),
                                   key=lambda x: sum(x[1]) / len(x[1]),
                                   reverse=True):
            avg = sum(pnls) / len(pnls)
            print(f"    {model.split('/')[-1]:<42} avg P&L: ${avg:>+9.2f}  (n={len(pnls)})")

    print(f"{'='*110}\n")


# ── Main ─────────────────────────────────────────────────────────────────────

async def run_batch(
    date: str,
    tickers: list[str] | None,
    speed: float,
    slippage_bps: float,
    grace_s: float,
    with_wire: bool,
    dry_run: bool = False,
):
    matrix = load_matrix()
    configs = build_configurations(matrix)

    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    print(f"\n{'='*70}")
    print(f"  CLIFFCLAW BATCH BACKTEST")
    print(f"  Batch ID:       {batch_id}")
    print(f"  Configurations: {len(configs)}")
    print(f"  Date:           {date}")
    print(f"  Tickers:        {', '.join(tickers) if tickers else 'from config'}")
    print(f"  Speed:          {'MAX' if speed == 0 else f'{speed}x'}")
    print(f"  Base model:     {matrix['base_model']}")
    print(f"  General models: {len(matrix['general_models'])} (rotating)")
    print(f"{'='*70}")

    if dry_run:
        print(f"\n  DRY RUN — printing configurations only:\n")
        for c in configs:
            print(
                f"  [{c['config_id']:>2}] {c['label']:<28} "
                f"thresh={c['conviction_threshold']:<4} "
                f"decay={c['markov_decay_factor']:<5} "
                f"window={c['markov_window_size']:<3} "
                f"model={c['general_model']}"
            )
        print(f"\n  Total: {len(configs)} configs × {len(matrix.get('replay_dates', [date]))} dates")
        return

    results = []

    for i, config in enumerate(configs):
        print(f"\n  ┌─ Config {config['config_id']}/{len(configs)}: {config['label']}")
        print(f"  │  Threshold={config['conviction_threshold']}, "
              f"Decay={config['markov_decay_factor']}, "
              f"Window={config['markov_window_size']}")
        print(f"  │  General model: {config['general_model']}")
        print(f"  └─ Running...", flush=True)

        try:
            result = await run_single_config(
                config=config,
                date=date,
                speed=speed,
                tickers=tickers,
                slippage_bps=slippage_bps,
                grace_s=grace_s,
                with_wire=with_wire,
            )
            results.append(result)
            _log_batch_result(batch_id, result)

            pnl_str = f"${result['total_pnl']:+,.2f}"
            print(
                f"     Done — {result['trades']} trades, "
                f"WR {result['win_rate']:.1f}%, "
                f"P&L {pnl_str}, "
                f"PF {result['profit_factor']:.2f} "
                f"({result['elapsed_s']:.0f}s)",
                flush=True,
            )
        except Exception as exc:
            print(f"     FAILED: {exc}", flush=True)
            traceback.print_exc()
            result = {**config, "date": date, "elapsed_s": 0,
                      "trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                      "total_pnl": 0, "avg_win": 0, "avg_loss": 0, "profit_factor": 0}
            results.append(result)

    # Export and summarize
    csv_path = _export_csv(batch_id, results)
    _print_summary(results)
    print(f"  CSV exported: {csv_path}")
    print(f"  Results stored in data/trades.db (batch_runs table, batch_id={batch_id})")


def main():
    parser = argparse.ArgumentParser(
        description="Run 27 Markov configurations through the CliffClaw backtester",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Replay date (YYYY-MM-DD). Defaults to first date in backtest_matrix.yaml",
    )
    parser.add_argument(
        "--tickers", type=str, default=None,
        help="Comma-separated tickers (default: from indicators.yaml)",
    )
    parser.add_argument(
        "--speed", type=float, default=0,
        help="Replay speed (0=MAX, default for batch)",
    )
    parser.add_argument(
        "--slippage", type=float, default=5.0,
        help="Slippage in bps (default: 5)",
    )
    parser.add_argument(
        "--grace", type=float, default=15.0,
        help="Grace period after replay (default: 15s for batch)",
    )
    parser.add_argument(
        "--with-wire", action="store_true",
        help="Enable Wire agent",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print configuration matrix without running backtests",
    )

    args = parser.parse_args()

    matrix = load_matrix()
    date = args.date or matrix.get("replay_dates", ["2026-03-24"])[0]

    tickers = (
        [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if args.tickers else None
    )

    try:
        asyncio.run(
            run_batch(
                date=date,
                tickers=tickers,
                speed=args.speed,
                slippage_bps=args.slippage,
                grace_s=args.grace,
                with_wire=args.with_wire,
                dry_run=args.dry_run,
            )
        )
    except KeyboardInterrupt:
        print("\n  Batch aborted by user.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
