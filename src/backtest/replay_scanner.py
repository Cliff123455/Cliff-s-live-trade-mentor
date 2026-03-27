# src/backtest/replay_scanner.py
# "The Time Machine" — replays historical 1-min bars as if they were live.
# Publishes to CH.MARKET_DATA in the exact same format as the real Scanner.

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from src.shared.base_agent import BaseAgent
from src.shared.constants import CH, AGENTS
from src.shared.sim_clock import set_sim_time_ms

load_dotenv()

_ET = ZoneInfo("America/New_York")


class ReplayScanner(BaseAgent):
    """Replays a historical trading day through the same market-data channel."""

    def __init__(self, replay_date: str, speed: float = 1.0, watchlist: list[str] | None = None):
        """
        Args:
            replay_date: Date to replay, e.g. "2026-03-24"
            speed: Playback speed multiplier (1.0 = real-time, 5.0 = 5x, 0 = max speed)
            watchlist: Tickers to replay. Defaults to config/indicators.yaml fallback_watchlist.
        """
        super().__init__(AGENTS.SCANNER)
        self.replay_date = replay_date
        self.speed = speed
        self.watchlist = watchlist or self._load_watchlist()

        self.data_client = StockHistoricalDataClient(
            api_key=os.getenv("ALPACA_API_KEY", ""),
            secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
        )

        self.bars_by_time: list[tuple[datetime, dict]] = []
        self.total_bars = 0
        self.bars_published = 0

    def _load_watchlist(self) -> list[str]:
        config_path = Path(__file__).resolve().parents[2] / "config" / "indicators.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        return cfg.get("fallback_watchlist", ["SPY", "QQQ"])

    async def _fetch_bars(self):
        """Fetch 1-min bars for the replay date from Alpaca."""
        date = datetime.strptime(self.replay_date, "%Y-%m-%d")
        start = date.replace(hour=9, minute=30, tzinfo=_ET)
        end = date.replace(hour=16, minute=0, tzinfo=_ET)

        self.log.info(
            "replay_fetching_bars",
            date=self.replay_date,
            tickers=self.watchlist,
        )

        req = StockBarsRequest(
            symbol_or_symbols=self.watchlist,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
        )

        bars = await asyncio.to_thread(self.data_client.get_stock_bars, req)

        all_bars = []
        for ticker, ticker_bars in bars.data.items():
            for bar in ticker_bars:
                ts = bar.timestamp
                msg = {
                    "ticker": ticker,
                    "timestamp_ms": int(ts.timestamp() * 1000),
                    "bid": float(bar.close),
                    "ask": float(bar.close) + 0.01,
                    "spread": 0.01,
                    "last": float(bar.close),
                    "volume_delta": int(bar.volume),
                    "tape_anomaly": False,
                    "sweep_detected": False,
                    "unusual_size_bid": False,
                    "unusual_size_ask": False,
                    "vwap": float(bar.vwap) if bar.vwap else float(bar.close),
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": int(bar.volume),
                    "_replay": True,
                    "_replay_date": self.replay_date,
                }
                all_bars.append((ts, msg))

        all_bars.sort(key=lambda x: x[0])
        self.bars_by_time = all_bars
        self.total_bars = len(all_bars)

        self.log.info("replay_bars_loaded", total_bars=self.total_bars, tickers=len(bars.data))

    async def run(self):
        """Stream historical bars through CH.MARKET_DATA at the configured speed."""
        await self._fetch_bars()

        if not self.bars_by_time:
            self.log.error("replay_no_bars", date=self.replay_date)
            return

        # Set sim clock to first bar time before announcing watchlist
        first_bar_ts_ms = self.bars_by_time[0][1]["timestamp_ms"]
        await set_sim_time_ms(self.redis, first_bar_ts_ms)

        # Announce watchlist
        await self.publish(CH.MARKET_DATA, {
            "ticker": "__WATCHLIST__",
            "timestamp_ms": first_bar_ts_ms,
            "bid": 0, "ask": 0, "spread": 0, "last": 0,
            "volume_delta": 0, "tape_anomaly": False,
            "sweep_detected": False, "unusual_size_bid": False, "unusual_size_ask": False,
            "_watchlist": self.watchlist,
            "_feed": "replay",
            "_source": "backtest",
            "_replay": True,
            "_replay_date": self.replay_date,
            "_replay_speed": self.speed,
        })

        self.log.info("replay_starting", date=self.replay_date, speed=f"{self.speed}x", total_bars=self.total_bars)

        first_ts = self.bars_by_time[0][0]
        replay_start_real = time.monotonic()

        for i, (bar_ts, msg) in enumerate(self.bars_by_time):
            if not self.running:
                break

            # Pace replay according to speed
            if self.speed > 0 and i > 0:
                elapsed_sim = (bar_ts - first_ts).total_seconds()
                elapsed_real = time.monotonic() - replay_start_real
                target_real = elapsed_sim / self.speed
                delay = target_real - elapsed_real
                if delay > 0:
                    await asyncio.sleep(delay)

            await self.publish(CH.MARKET_DATA, msg)
            self.bars_published = i + 1

            await set_sim_time_ms(self.redis, msg["timestamp_ms"])

            if (i + 1) % 100 == 0:
                pct = round((i + 1) / self.total_bars * 100, 1)
                self.log.info("replay_progress", bars=i + 1, total=self.total_bars, pct=pct, sim_time=bar_ts.strftime("%H:%M:%S"))

        self.log.info("replay_complete", bars_published=self.bars_published, total=self.total_bars)

        # Signal replay done
        await self.publish(CH.MARKET_DATA, {
            "ticker": "__REPLAY_DONE__",
            "bid": 0, "ask": 0, "spread": 0, "last": 0,
            "volume_delta": 0, "tape_anomaly": False,
            "sweep_detected": False, "unusual_size_bid": False, "unusual_size_ask": False,
            "_replay": True,
            "_replay_date": self.replay_date,
            "_bars_published": self.bars_published,
        })
