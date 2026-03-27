# main.py
# ScalpBot / CliffClaw — Multi-Agent Scalping System
# Starts all 8 agents concurrently + the Flask UI dashboard.

import asyncio
import os
import sys
import threading
from dotenv import load_dotenv

load_dotenv()

# ── Agent imports ─────────────────────────────────────────────────────────────
from src.agents.market_data_agent import MarketDataAgent
from src.agents.news_sentiment_agent import NewsSentimentAgent
from src.agents.technical_analysis_agent import TechnicalAnalysisAgent
from src.agents.risk_modeling_agent import RiskModelingAgent
from src.agents.performance_agent import PerformanceAgent
from src.coordinator.trade_coordinator import TradeCoordinator
from src.execution.order_execution_agent import OrderExecutionAgent
from src.execution.position_monitor_agent import PositionMonitorAgent

# ── UI import ─────────────────────────────────────────────────────────────────
from ui.app import start_ui

BANNER = r"""
  ___    __    ____  ____  ____  ____  __   _        __  ____  _   _
 / __)  (  )  (_  _)( ___)( ___)( ___)(  ) ( )      / _)(  _ \( ) ( )
( (__    )(__  _)(_  )__)  )__)  )__)  )(__ \/ \_/\_( (/\ )   / )\_/ (
 \___)  (____)(____)(__)  (__)  (____)(____)(__/\___/ \__/(_)\_)\___/

 ScalpBot // CliffClaw — Multi-Agent Scalping System
 ─────────────────────────────────────────────────────────
 Dashboard → http://127.0.0.1:{port}
 Press Ctrl+C to stop all agents
"""


async def run_agent(agent, name: str):
    """Wrap an agent's start() so one crash doesn't kill the whole system."""
    try:
        print(f"  ▶  Starting {name}...")
        await agent.start()
    except Exception as e:
        print(f"  ✗  {name} crashed: {e}", file=sys.stderr)
        try:
            await agent.stop()
        except Exception:
            pass


async def main():
    agents = [
        (MarketDataAgent(),       "The Scanner   [Tier 1]"),
        (NewsSentimentAgent(),    "The Wire      [Tier 1]"),
        (TechnicalAnalysisAgent(),"The Chartist  [Tier 2]"),
        (RiskModelingAgent(),     "The Actuary   [Tier 2]"),
        (TradeCoordinator(),      "The General   [Tier 3]"),
        (OrderExecutionAgent(),   "The Sniper    [Tier 4]"),
        (PositionMonitorAgent(),  "The Watcher   [Tier 4]"),
        (PerformanceAgent(),      "The Historian [Tier 5]"),
    ]

    ui_port = int(os.getenv("UI_PORT", 5000))
    print(BANNER.format(port=ui_port))

    tasks = [asyncio.create_task(run_agent(agent, name)) for agent, name in agents]

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        print("\n  Shutdown signal received — stopping agents...")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    ui_port = int(os.getenv("UI_PORT", 5000))

    # Start Flask UI in a background daemon thread
    ui_thread = threading.Thread(target=start_ui, daemon=True, name="ScalpBot-UI")
    ui_thread.start()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  ScalpBot stopped. P&L is in the books.")
        sys.exit(0)
