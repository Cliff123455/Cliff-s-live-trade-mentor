# agents_runner.py
# Starts all ScalpBot agents in either LIVE or BACKTEST mode.
# Launched as a subprocess by the UI's "Launch Agents" button
# so the Flask server stays responsive.
#
# Usage:
#   python agents_runner.py                                    # Live mode
#   python agents_runner.py --mode backtest --date 2026-03-24  # Backtest mode
#   python agents_runner.py --mode backtest --date 2026-03-24 --speed 10

import argparse
import asyncio
import json
import os
import sys
import traceback
from dotenv import load_dotenv

load_dotenv()


async def _start_agent(AgentClass, name: str, **kwargs):
    """Start one agent, print full traceback on crash, keep others running."""
    try:
        print(f"[runner] Starting {name}...", flush=True)
        agent = AgentClass(**kwargs) if kwargs else AgentClass()
        await agent.start()
        print(f"[runner] {name} exited normally.", flush=True)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"\n[runner] *** {name} CRASHED ***", flush=True)
        traceback.print_exc()
        print(f"[runner] *** end {name} crash ***\n", flush=True)


async def _wait_for_replay_done(redis_url: str, tasks: list, grace_s: float):
    """Subscribe to market data, wait for __REPLAY_DONE__, then shut down."""
    import redis.asyncio as aioredis
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
                print(f"\n[runner] Replay complete — {bars} bars published.", flush=True)
                print(f"[runner] Waiting {int(grace_s)}s for in-flight trades...", flush=True)
                await asyncio.sleep(grace_s)
                print("[runner] Stopping agents...", flush=True)
                for t in tasks:
                    t.cancel()
                break
    except asyncio.CancelledError:
        pass
    finally:
        await ps.aclose()
        await r.aclose()


async def _clean_state(redis_url: str):
    """Flush backtest-relevant Redis keys for a fresh run."""
    import redis.asyncio as aioredis
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
    await r.aclose()


async def main_live():
    """Start all 8 agents in LIVE mode (original behavior)."""
    from src.agents.market_data_agent       import MarketDataAgent
    from src.agents.news_sentiment_agent    import NewsSentimentAgent
    from src.agents.technical_analysis_agent import TechnicalAnalysisAgent
    from src.agents.risk_modeling_agent     import RiskModelingAgent
    from src.agents.performance_agent       import PerformanceAgent
    from src.coordinator.trade_coordinator  import TradeCoordinator
    from src.execution.order_execution_agent import OrderExecutionAgent
    from src.execution.position_monitor_agent import PositionMonitorAgent
    from src.agents.market_pulse_agent import MarketPulseAgent
    from src.agents.markov_regime_agent import MarkovRegimeAgent

    agent_classes = [
        (MarketPulseAgent,       "The Pulse",     {}),
        (MarkovRegimeAgent,      "The Oracle",    {}),
        (MarketDataAgent,        "The Scanner",   {}),
        (NewsSentimentAgent,     "The Wire",      {}),
        (TechnicalAnalysisAgent, "The Chartist",  {}),
        (RiskModelingAgent,      "The Actuary",   {}),
        (TradeCoordinator,       "The General",   {}),
        (OrderExecutionAgent,    "The Sniper",    {}),
        (PositionMonitorAgent,   "The Watcher",   {}),
        (PerformanceAgent,       "The Historian", {}),
    ]

    tasks = [
        asyncio.create_task(_start_agent(cls, name, **kwargs))
        for cls, name, kwargs in agent_classes
    ]

    print(f"[runner] LIVE mode — {len(tasks)} agents launched.", flush=True)

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("[runner] Shutdown signal received.", flush=True)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    print("[runner] All agents stopped.", flush=True)


async def main_backtest(date: str, speed: float, tickers: list[str] | None,
                        with_wire: bool, grace_s: float, slippage_bps: float):
    """Start agents in BACKTEST mode — replay historical data."""
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

    print(f"\n{'='*60}", flush=True)
    print(f"  CliffClaw Backtest", flush=True)
    print(f"  Date:     {date}", flush=True)
    print(f"  Speed:    {'MAX' if speed == 0 else f'{speed}x'}", flush=True)
    print(f"  Tickers:  {', '.join(tickers) if tickers else 'from config'}", flush=True)
    print(f"  Slippage: {slippage_bps} bps", flush=True)
    print(f"{'='*60}\n", flush=True)

    print("[runner] Cleaning Redis state...", flush=True)
    await _clean_state(redis_url)

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
        (ReplayScanner,          "The Scanner (Replay)", {"replay_date": date, "speed": speed, "watchlist": tickers}),
        (TechnicalAnalysisAgent, "The Chartist",         {}),
        (RiskModelingAgent,      "The Actuary",          {}),
        (TradeCoordinator,       "The General",          {}),
        (PaperSniper,            "The Sniper (Paper)",   {"slippage_bps": slippage_bps}),
        (PositionMonitorAgent,   "The Watcher",          {}),
        (PerformanceAgent,       "The Historian",        {}),
    ]

    if with_wire:
        from src.agents.news_sentiment_agent import NewsSentimentAgent
        agent_specs.insert(1, (NewsSentimentAgent, "The Wire", {}))

    print("[runner] Launching agents:", flush=True)
    tasks = [
        asyncio.create_task(_start_agent(cls, name, **kwargs))
        for cls, name, kwargs in agent_specs
    ]

    # Monitor for replay completion
    monitor = asyncio.create_task(
        _wait_for_replay_done(redis_url, tasks, grace_s)
    )

    print(f"[runner] BACKTEST mode — {len(tasks)} agents running.\n", flush=True)

    try:
        await asyncio.gather(monitor, *tasks, return_exceptions=True)
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("[runner] Shutdown signal received.", flush=True)
        for t in tasks:
            t.cancel()
        monitor.cancel()
        await asyncio.gather(monitor, *tasks, return_exceptions=True)

    print("[runner] All agents stopped.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="CliffClaw Agent Runner")
    parser.add_argument("--mode", choices=["live", "backtest"], default="live",
                        help="Operating mode (default: live)")
    parser.add_argument("--date", type=str, default=None,
                        help="Backtest date (YYYY-MM-DD), required for backtest mode")
    parser.add_argument("--speed", type=float, default=10.0,
                        help="Backtest replay speed (1=real-time, 10=default, 0=max)")
    parser.add_argument("--tickers", type=str, default=None,
                        help="Comma-separated tickers (backtest only)")
    parser.add_argument("--with-wire", action="store_true",
                        help="Enable Wire agent in backtest (disabled by default)")
    parser.add_argument("--grace", type=float, default=30.0,
                        help="Post-replay grace period in seconds (default: 30)")
    parser.add_argument("--slippage", type=float, default=5.0,
                        help="Simulated slippage in bps (default: 5)")

    args = parser.parse_args()

    if args.mode == "backtest":
        if not args.date:
            parser.error("--date is required for backtest mode")
        tickers = (
            [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
            if args.tickers else None
        )
        asyncio.run(main_backtest(
            date=args.date,
            speed=args.speed,
            tickers=tickers,
            with_wire=args.with_wire,
            grace_s=args.grace,
            slippage_bps=args.slippage,
        ))
    else:
        asyncio.run(main_live())


if __name__ == "__main__":
    main()
