# backtest_runner.py — Replay a historical trading day through the full pipeline.
#
# Usage:
#   python backtest_runner.py --date 2026-03-20 --speed 10
#   python backtest_runner.py --date 2026-03-20 --speed 0 --tickers AAPL,MSFT,NVDA
#   python backtest_runner.py --date 2026-03-20 --speed 5 --with-wire
#
# Speed: 1=real-time, 5=5x, 10=10x (default), 0=max (no delay)

# ── Fix Windows Store Python SSL permission error ────────────────────────────
# The Windows Store Python build tries to write an SSL keylog to a sandboxed
# virtual file that it can't access. Clearing the env var prevents this.
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
import json
import logging
import sqlite3
import sys
import time
import traceback
import warnings
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Suppress noisy pandas_ta debug output (ATR series prints etc.)
logging.getLogger("pandas_ta").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", category=FutureWarning, module="pandas_ta")

# Ensure CWD is project root so relative config paths resolve correctly
os.chdir(Path(__file__).resolve().parent)

import redis.asyncio as aioredis


# ── Agent launcher ───────────────────────────────────────────────────────────

async def _start_agent(AgentClass, name: str, **kwargs):
    """Start one agent; print traceback on crash, keep others running."""
    try:
        agent = AgentClass(**kwargs) if kwargs else AgentClass()
        print(f"  [+] {name}", flush=True)
        await agent.start()
    except asyncio.CancelledError:
        pass
    except Exception:
        print(f"\n  [!] {name} CRASHED:", flush=True)
        traceback.print_exc()
        print(flush=True)


# ── Replay completion monitor ────────────────────────────────────────────────

async def _wait_for_replay_done(redis_url: str, tasks: list, grace_s: float):
    """Subscribe to market data, wait for __REPLAY_DONE__, then shut down."""
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
                bars = data.get("_bars_published", 0)
                print(
                    f"\n  Replay complete — {bars} bars published.",
                    flush=True,
                )
                print(
                    f"  Waiting {int(grace_s)}s for in-flight trades to settle...",
                    flush=True,
                )
                await asyncio.sleep(grace_s)
                print("  Stopping agents...", flush=True)
                for t in tasks:
                    t.cancel()
                break
    except asyncio.CancelledError:
        pass
    finally:
        await ps.aclose()
        await r.aclose()


# ── Redis cleanup ────────────────────────────────────────────────────────────

async def _clean_state(redis_url: str):
    """Flush backtest-relevant Redis keys for a fresh run."""
    r = aioredis.from_url(redis_url, decode_responses=True)
    keys = [
        "state:positions",
        "state:pending-orders",
        "state:daily-pnl",
        "state:risk-params",
        "state:setups",
        "state:setup-weights",
        "state:trade-log",
        "state:sim-clock",
        "state:market-regime",
        "state:markov",
    ]
    for key in keys:
        await r.delete(key)
    # Also clear per-ticker market/catalyst state
    async for key in r.scan_iter("state:market:*"):
        await r.delete(key)
    async for key in r.scan_iter("state:catalyst:*"):
        await r.delete(key)
    async for key in r.scan_iter("state:agent-status:*"):
        await r.delete(key)
    await r.aclose()


# ── Clear old backtest trades from DB ────────────────────────────────────────

def _clear_trades_db():
    """Delete all rows from trades.db so results only reflect this run."""
    db_path = Path(__file__).parent / "data" / "trades.db"
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM trades")
    conn.commit()
    conn.close()


# ── Main backtest orchestrator ───────────────────────────────────────────────

async def run_backtest(
    date: str,
    speed: float,
    tickers: list[str] | None,
    with_wire: bool,
    grace_s: float,
    slippage_bps: float,
):
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

    # ── Pre-flight ───────────────────────────────────────────────────────
    print(f"\n{'='*60}", flush=True)
    print(f"  CliffClaw Backtest", flush=True)
    print(f"  Date:     {date}", flush=True)
    print(f"  Speed:    {'MAX' if speed == 0 else f'{speed}x'}", flush=True)
    print(f"  Tickers:  {', '.join(tickers) if tickers else 'from config'}", flush=True)
    print(f"  Wire:     {'ON' if with_wire else 'OFF (neutral catalyst baseline)'}", flush=True)
    print(f"  Slippage: {slippage_bps} bps", flush=True)
    print(f"  Grace:    {int(grace_s)}s after replay ends", flush=True)
    print(f"{'='*60}\n", flush=True)

    print("  Cleaning Redis state...", flush=True)
    await _clean_state(redis_url)
    _clear_trades_db()

    # ── Suppress pandas_ta verbose stdout (ATR series prints) ──────────
    try:
        import pandas_ta
        pandas_ta.verbose = False
    except Exception:
        pass

    # ── Build agent list ─────────────────────────────────────────────────
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
        (MarkovRegimeAgent,      "The Oracle",           {}),
        (ReplayScanner, "The Scanner (Replay)", {"replay_date": date, "speed": speed, "watchlist": tickers}),
        (TechnicalAnalysisAgent, "The Chartist", {}),
        (RiskModelingAgent, "The Actuary", {}),
        (TradeCoordinator, "The General", {}),
        (PaperSniper, "The Sniper (Paper)", {"slippage_bps": slippage_bps}),
        (PositionMonitorAgent, "The Watcher", {}),
        (PerformanceAgent, "The Historian", {}),
    ]

    if with_wire:
        from src.agents.news_sentiment_agent import NewsSentimentAgent
        agent_specs.insert(1, (NewsSentimentAgent, "The Wire", {}))

    # ── Launch agents ────────────────────────────────────────────────────
    print("  Launching agents:", flush=True)
    tasks = [
        asyncio.create_task(_start_agent(cls, name, **kwargs))
        for cls, name, kwargs in agent_specs
    ]

    # ── Monitor for replay completion ────────────────────────────────────
    monitor = asyncio.create_task(
        _wait_for_replay_done(redis_url, tasks, grace_s)
    )

    print(f"\n  {len(tasks)} agents running. Ctrl+C to abort.\n", flush=True)

    try:
        await asyncio.gather(monitor, *tasks, return_exceptions=True)
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("\n  Shutting down...", flush=True)
        for t in tasks:
            t.cancel()
        monitor.cancel()
        await asyncio.gather(monitor, *tasks, return_exceptions=True)

    # ── Results ──────────────────────────────────────────────────────────
    _print_results(date)
    report_path = _generate_report_html(date)
    _log_run(date, speed, tickers, slippage_bps, with_wire, report_path)
    _print_run_history()


# ── Results printer ──────────────────────────────────────────────────────────

def _print_results(date: str):
    db_path = Path(__file__).parent / "data" / "trades.db"
    if not db_path.exists():
        print("\n  No trades.db found — no trades were recorded.\n")
        return

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT ticker, setup_type, direction, entry_price, exit_price, "
        "shares, pnl, exit_reason FROM trades ORDER BY id"
    ).fetchall()
    conn.close()

    print(f"\n{'='*60}")
    print(f"  BACKTEST RESULTS — {date}")
    print(f"{'='*60}")

    if not rows:
        print("  No trades executed.")
        print(f"{'='*60}\n")
        return

    total_pnl = sum(r[6] or 0 for r in rows)
    wins = [r for r in rows if (r[6] or 0) > 0]
    losses = [r for r in rows if (r[6] or 0) <= 0]
    win_rate = len(wins) / len(rows) * 100 if rows else 0

    print(f"  Trades:    {len(rows)}")
    print(f"  Wins:      {len(wins)}")
    print(f"  Losses:    {len(losses)}")
    print(f"  Win Rate:  {win_rate:.1f}%")
    print(f"  Total P&L: ${total_pnl:+,.2f}")

    if wins:
        avg_win = sum(r[6] for r in wins) / len(wins)
        print(f"  Avg Win:   ${avg_win:+,.2f}")
    if losses:
        avg_loss = sum(r[6] for r in losses) / len(losses)
        print(f"  Avg Loss:  ${avg_loss:+,.2f}")

    print(f"\n  {'Ticker':<7} {'Dir':<6} {'Entry':>8} {'Exit':>8} {'Shares':>6} {'P&L':>10}  Exit Reason")
    print(f"  {'-'*6:<7} {'-'*5:<6} {'-'*8:>8} {'-'*8:>8} {'-'*6:>6} {'-'*10:>10}  {'-'*20}")

    for ticker, setup_type, direction, entry_px, exit_px, shares, pnl, reason in rows:
        pnl = pnl or 0
        entry_px = entry_px or 0
        exit_px = exit_px or 0
        shares = shares or 0
        print(
            f"  {ticker or '???':<7} {(direction or '?'):<6} "
            f"${entry_px:>7.2f} ${exit_px:>7.2f} {shares:>6} "
            f"${pnl:>+9.2f}  {reason or 'unknown'}"
        )

    print(f"\n{'='*60}\n")


# ── HTML report generator ─────────────────────────────────────────────────────

def _generate_report_html(date: str) -> str | None:
    """Generate a self-contained HTML report and return its path."""
    import webbrowser
    from datetime import datetime as _dt

    db_path = Path(__file__).parent / "data" / "trades.db"
    if not db_path.exists():
        return None

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT ticker, setup_type, direction, entry_price, exit_price, "
        "shares, pnl, exit_reason, entry_ts, exit_ts FROM trades ORDER BY id"
    ).fetchall()
    conn.close()

    if not rows:
        return None

    total_pnl = sum(r[6] or 0 for r in rows)
    wins = [r for r in rows if (r[6] or 0) > 0]
    losses = [r for r in rows if (r[6] or 0) <= 0]
    win_rate = len(wins) / len(rows) * 100 if rows else 0
    avg_win = sum(r[6] for r in wins) / len(wins) if wins else 0
    avg_loss = sum(r[6] for r in losses) / len(losses) if losses else 0
    gross_wins = sum(r[6] for r in wins) if wins else 0
    gross_losses = abs(sum(r[6] for r in losses)) if losses else 0
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else float('inf')

    # Build cumulative P&L series
    cum_pnl = []
    running = 0
    for r in rows:
        running += r[6] or 0
        cum_pnl.append(round(running, 2))

    # Trade table rows
    trade_rows_html = ""
    for i, (ticker, stype, direction, entry_px, exit_px, shares, pnl, reason, ets, xts) in enumerate(rows):
        pnl = pnl or 0
        color = "#00ff88" if pnl > 0 else "#ff4466"
        trade_rows_html += (
            f"<tr><td>{i+1}</td><td>{ticker}</td><td>{stype}</td><td>{direction}</td>"
            f"<td>${entry_px or 0:.2f}</td><td>${exit_px or 0:.2f}</td><td>{shares or 0}</td>"
            f"<td style='color:{color}'>${pnl:+,.2f}</td><td>{reason or 'unknown'}</td></tr>\n"
        )

    labels = json.dumps(list(range(1, len(cum_pnl) + 1)))
    data_points = json.dumps(cum_pnl)
    pnl_color = "#00ff88" if total_pnl >= 0 else "#ff4466"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>CliffClaw Backtest — {date}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body {{ background:#0a0e17; color:#c0c8d8; font-family:'Courier New',monospace; margin:20px; }}
  h1 {{ color:#00d4ff; }} h2 {{ color:#7b8db0; }}
  .stats {{ display:flex; gap:20px; flex-wrap:wrap; margin:20px 0; }}
  .stat {{ background:#111827; border:1px solid #1e2a3a; border-radius:8px; padding:15px 25px; }}
  .stat .label {{ font-size:12px; color:#7b8db0; }} .stat .value {{ font-size:24px; font-weight:bold; }}
  table {{ border-collapse:collapse; width:100%; margin:20px 0; }}
  th,td {{ border:1px solid #1e2a3a; padding:8px 12px; text-align:right; }}
  th {{ background:#111827; color:#00d4ff; }} td:first-child,th:first-child {{ text-align:center; }}
  td:nth-child(2),td:nth-child(3),td:nth-child(4),td:nth-child(9) {{ text-align:left; }}
  canvas {{ max-height:350px; }}
</style></head><body>
<h1>CLIFFCLAW // BACKTEST — {date}</h1>
<div class="stats">
  <div class="stat"><div class="label">Total P&L</div><div class="value" style="color:{pnl_color}">${total_pnl:+,.2f}</div></div>
  <div class="stat"><div class="label">Trades</div><div class="value">{len(rows)}</div></div>
  <div class="stat"><div class="label">Win Rate</div><div class="value">{win_rate:.1f}%</div></div>
  <div class="stat"><div class="label">Avg Win</div><div class="value" style="color:#00ff88">${avg_win:+,.2f}</div></div>
  <div class="stat"><div class="label">Avg Loss</div><div class="value" style="color:#ff4466">${avg_loss:+,.2f}</div></div>
  <div class="stat"><div class="label">Profit Factor</div><div class="value">{profit_factor}</div></div>
</div>
<h2>EQUITY CURVE</h2>
<canvas id="eq"></canvas>
<h2>TRADE LOG</h2>
<table><thead><tr><th>#</th><th>Ticker</th><th>Setup</th><th>Dir</th><th>Entry</th><th>Exit</th><th>Shares</th><th>P&L</th><th>Exit Reason</th></tr></thead>
<tbody>{trade_rows_html}</tbody></table>
<script>
new Chart(document.getElementById('eq'),{{type:'line',data:{{labels:{labels},datasets:[{{
  label:'Cumulative P&L ($)',data:{data_points},borderColor:'#00d4ff',backgroundColor:'rgba(0,212,255,0.1)',
  fill:true,tension:0.3,pointRadius:4,pointBackgroundColor:{data_points}.map(v=>v>=0?'#00ff88':'#ff4466')
}}]}},options:{{responsive:true,plugins:{{legend:{{labels:{{color:'#c0c8d8'}}}}}},
scales:{{x:{{title:{{display:true,text:'Trade #',color:'#7b8db0'}},ticks:{{color:'#7b8db0'}},grid:{{color:'#1e2a3a'}}}},
y:{{title:{{display:true,text:'P&L ($)',color:'#7b8db0'}},ticks:{{color:'#7b8db0'}},grid:{{color:'#1e2a3a'}}}}}}
}}}});
</script></body></html>"""

    reports_dir = Path(__file__).parent / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    report_path = reports_dir / f"backtest_{date}_{ts}.html"
    report_path.write_text(html, encoding="utf-8")

    print(f"  Report saved: {report_path}")
    try:
        webbrowser.open(str(report_path))
    except Exception:
        pass

    return str(report_path)


# ── Backtest run logging ──────────────────────────────────────────────────────

def _ensure_runs_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_timestamp   TEXT,
            replay_date     TEXT,
            speed           REAL,
            tickers         TEXT,
            slippage_bps    REAL,
            with_wire       INTEGER,
            config_snapshot TEXT,
            total_trades    INTEGER,
            wins            INTEGER,
            losses          INTEGER,
            win_rate        REAL,
            total_pnl       REAL,
            avg_win         REAL,
            avg_loss        REAL,
            profit_factor   REAL,
            report_path     TEXT
        )
    """)
    conn.commit()


def _snapshot_config() -> str:
    """Read all YAML config files and return a merged JSON string."""
    config_dir = Path(__file__).parent / "config"
    snapshot = {}
    for yaml_file in sorted(config_dir.glob("*.yaml")):
        try:
            with open(yaml_file) as f:
                import yaml
                snapshot[yaml_file.stem] = yaml.safe_load(f)
        except Exception:
            snapshot[yaml_file.stem] = "error reading"
    return json.dumps(snapshot, default=str)


def _log_run(date: str, speed: float, tickers: list[str] | None,
             slippage_bps: float, with_wire: bool, report_path: str | None):
    """Log this backtest run with config + results to SQLite."""
    from datetime import datetime as _dt

    db_path = Path(__file__).parent / "data" / "trades.db"
    conn = sqlite3.connect(str(db_path))
    _ensure_runs_table(conn)

    # Compute stats from trades table
    rows = conn.execute("SELECT pnl FROM trades ORDER BY id").fetchall()
    total_trades = len(rows)
    wins = [r[0] for r in rows if (r[0] or 0) > 0]
    losses = [r[0] for r in rows if (r[0] or 0) <= 0]
    total_pnl = sum(r[0] or 0 for r in rows)
    win_rate = len(wins) / total_trades * 100 if total_trades > 0 else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    gross_wins = sum(wins) if wins else 0
    gross_losses = abs(sum(losses)) if losses else 0
    profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 0

    conn.execute(
        "INSERT INTO backtest_runs "
        "(run_timestamp, replay_date, speed, tickers, slippage_bps, with_wire, "
        "config_snapshot, total_trades, wins, losses, win_rate, total_pnl, "
        "avg_win, avg_loss, profit_factor, report_path) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _dt.now().isoformat(),
            date,
            speed,
            ",".join(tickers) if tickers else "config",
            slippage_bps,
            int(with_wire),
            _snapshot_config(),
            total_trades,
            len(wins),
            len(losses),
            round(win_rate, 1),
            round(total_pnl, 2),
            round(avg_win, 2),
            round(avg_loss, 2),
            profit_factor,
            report_path,
        ),
    )
    conn.commit()
    conn.close()


def _print_run_history():
    """Print a comparison table of recent backtest runs."""
    db_path = Path(__file__).parent / "data" / "trades.db"
    if not db_path.exists():
        return

    conn = sqlite3.connect(str(db_path))
    try:
        runs = conn.execute(
            "SELECT replay_date, speed, tickers, slippage_bps, "
            "total_trades, wins, losses, win_rate, total_pnl, profit_factor "
            "FROM backtest_runs ORDER BY id DESC LIMIT 10"
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return
    conn.close()

    if not runs:
        return

    runs = list(reversed(runs))  # Show oldest first

    print(f"\n{'='*90}")
    print(f"  BACKTEST RUN HISTORY (last {len(runs)} runs)")
    print(f"{'='*90}")
    print(f"  {'Date':<12} {'Speed':>6} {'Tickers':<14} {'Slip':>5} {'Trades':>7} {'W/L':>6} {'WR%':>6} {'P&L':>11} {'PF':>5}")
    print(f"  {'-'*11:<12} {'-'*6:>6} {'-'*13:<14} {'-'*5:>5} {'-'*7:>7} {'-'*6:>6} {'-'*6:>6} {'-'*11:>11} {'-'*5:>5}")

    for rd, spd, tickers, slip, trades, w, l, wr, pnl, pf in runs:
        tickers_short = (tickers or "config")[:13]
        spd_str = "MAX" if spd == 0 else f"{spd}x"
        print(
            f"  {rd:<12} {spd_str:>6} {tickers_short:<14} {slip:>5.0f} "
            f"{trades:>7} {w:>2}/{l:<3} {wr:>5.1f}% ${pnl:>+9.2f} {pf:>5.2f}"
        )

    print(f"{'='*90}\n")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="CliffClaw Backtest — replay a historical trading day",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python backtest_runner.py --date 2026-03-20 --speed 10\n"
            "  python backtest_runner.py --date 2026-03-20 --speed 0 --tickers AAPL,MSFT\n"
            "  python backtest_runner.py --date 2026-03-20 --speed 5 --with-wire\n"
        ),
    )
    parser.add_argument(
        "--date", required=True,
        help="Date to replay (YYYY-MM-DD, must be a trading day)",
    )
    parser.add_argument(
        "--speed", type=float, default=10.0,
        help="Playback speed multiplier (1=real-time, 10=default, 0=max)",
    )
    parser.add_argument(
        "--tickers", type=str, default=None,
        help="Comma-separated tickers to replay (default: from indicators.yaml)",
    )
    parser.add_argument(
        "--with-wire", action="store_true",
        help="Enable the Wire (news/social) agent (disabled by default in backtest)",
    )
    parser.add_argument(
        "--grace", type=float, default=30.0,
        help="Seconds to wait after replay ends for trades to settle (default: 30)",
    )
    parser.add_argument(
        "--slippage", type=float, default=5.0,
        help="Simulated slippage in basis points (default: 5 = 0.05%%)",
    )

    args = parser.parse_args()

    # Normalize date: accept MM/DD/YYYY, M/D/YYYY, or YYYY-MM-DD
    raw_date = args.date.strip()
    if "/" in raw_date:
        from datetime import datetime as _dt
        try:
            parsed = _dt.strptime(raw_date, "%m/%d/%Y")
        except ValueError:
            parsed = _dt.strptime(raw_date, "%m/%d/%y")
        raw_date = parsed.strftime("%Y-%m-%d")
    normalized_date = raw_date

    tickers = (
        [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if args.tickers
        else None
    )

    try:
        asyncio.run(
            run_backtest(
                date=normalized_date,
                speed=args.speed,
                tickers=tickers,
                with_wire=args.with_wire,
                grace_s=args.grace,
                slippage_bps=args.slippage,
            )
        )
    except KeyboardInterrupt:
        print("\n  Backtest aborted by user.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
