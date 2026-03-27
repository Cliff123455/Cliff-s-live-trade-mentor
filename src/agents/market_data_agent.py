# src/agents/market_data_agent.py
# "The Scanner" — dynamic screener + Alpaca live stream → Redis pub/sub
#
# At startup: calls Alpaca's most-actives screener, filters by price ($10-$90)
# and trade count, picks top 20. Refreshes every 30 min. Falls back to a
# static list if the screener fails.

import asyncio
import os
import time
from collections import defaultdict

import yaml
from dotenv import load_dotenv

from alpaca.data.live import StockDataStream
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import MostActivesRequest, StockLatestQuoteRequest

from src.shared.base_agent import BaseAgent
from src.shared.constants import AGENTS, CH, SK
import json

load_dotenv()


def _load_config() -> dict:
    with open("config/indicators.yaml", "r") as fh:
        return yaml.safe_load(fh)


class MarketDataAgent(BaseAgent):
    """The Scanner — dynamically screens top movers and streams live quotes."""

    def __init__(self):
        super().__init__(AGENTS.SCANNER)
        self._cfg = _load_config()
        self._screener_cfg = self._cfg.get("screener", {})
        self._fallback = [str(t).upper() for t in self._cfg.get("fallback_watchlist", [
            "NVDA", "TSLA", "AAPL", "AMD", "INTC", "SMCI", "SOXL", "TQQQ", "SPY", "QQQ"
        ])]
        self.watchlist: list[str] = list(self._fallback)

        # Per-ticker rolling state
        self._last: dict[str, float] = defaultdict(float)
        self._volume_delta: dict[str, int] = defaultdict(int)
        self._ask_size_stats: dict[str, list] = defaultdict(lambda: [0.0, 0])
        self._bid_size_stats: dict[str, list] = defaultdict(lambda: [0.0, 0])

        self._stream: StockDataStream | None = None

    # ── Screener ──────────────────────────────────────────────────────────────

    async def _build_watchlist(self) -> list[str]:
        """Call Alpaca screener and return filtered top-N symbols."""
        scfg = self._screener_cfg
        if not scfg.get("enabled", True):
            return self._fallback

        min_price    = float(scfg.get("min_price", 10.0))
        max_price    = float(scfg.get("max_price", 90.0))
        candidates   = int(scfg.get("candidates", 50))
        top_n        = int(scfg.get("top_n", 20))
        min_trades   = int(scfg.get("min_trade_count", 100_000))

        key = os.getenv("ALPACA_API_KEY", "")
        sec = os.getenv("ALPACA_SECRET_KEY", "")

        try:
            # Step 1: most-actives by trade count
            sc = ScreenerClient(key, sec)
            res = await asyncio.to_thread(
                sc.get_most_actives,
                MostActivesRequest(top=candidates, by="trades")
            )
            candidates_list = [
                s for s in res.most_actives
                if (s.trade_count or 0) >= min_trades
            ]
            symbols = [s.symbol for s in candidates_list]
            tc_map  = {s.symbol: s.trade_count for s in candidates_list}

            if not symbols:
                raise ValueError("Screener returned 0 candidates above min_trade_count")

            # Step 2: get current prices to filter by range
            hc = StockHistoricalDataClient(key, sec)
            quotes = await asyncio.to_thread(
                hc.get_stock_latest_quote,
                StockLatestQuoteRequest(symbol_or_symbols=symbols)
            )

            filtered = []
            for sym in symbols:
                q = quotes.get(sym)
                if not q or not q.ask_price or not q.bid_price:
                    continue
                mid = (q.bid_price + q.ask_price) / 2.0
                if min_price <= mid <= max_price:
                    filtered.append((sym, tc_map.get(sym, 0)))

            # Sort by trade count descending, take top N
            filtered.sort(key=lambda x: x[1], reverse=True)
            result = [sym for sym, _ in filtered[:top_n]]

            if len(result) < 5:
                # Merge screener results with fallback list (deduped, screener first)
                self.log.info("screener_few_results_merging", screener_count=len(result))
                merged = list(result)
                for sym in self._fallback:
                    if sym not in merged:
                        merged.append(sym)
                result = merged

            self.log.info("screener_complete", watchlist=result, count=len(result),
                          price_range=f"${min_price}-${max_price}")
            return result

        except Exception as exc:
            self.log.error("screener_failed", error=str(exc), using_fallback=True)
            return self._fallback

    # ── Alpaca async stream callbacks ─────────────────────────────────────────

    async def _on_quote(self, quote) -> None:
        try:
            ticker   = str(quote.symbol).upper()
            bid      = float(quote.bid_price or 0.0)
            ask      = float(quote.ask_price or 0.0)
            bid_size = float(quote.bid_size  or 0.0)
            ask_size = float(quote.ask_size  or 0.0)
            spread   = round(ask - bid, 4) if ask > 0 and bid > 0 else 0.0

            a = self._ask_size_stats[ticker]; a[0] += ask_size; a[1] += 1
            b = self._bid_size_stats[ticker]; b[0] += bid_size; b[1] += 1
            avg_ask = a[0] / a[1] if a[1] > 0 else ask_size
            avg_bid = b[0] / b[1] if b[1] > 0 else bid_size

            payload = {
                "ticker":           ticker,
                "timestamp_ms":     int(time.time() * 1000),
                "bid":              bid,
                "ask":              ask,
                "spread":           spread,
                "last":             self._last[ticker],
                "volume_delta":     self._volume_delta[ticker],
                "tape_anomaly":     False,
                "sweep_detected":   False,
                "unusual_size_bid": bid_size > avg_bid * 5.0 if avg_bid > 0 else False,
                "unusual_size_ask": ask_size > avg_ask * 5.0 if avg_ask > 0 else False,
            }
            await self.publish(CH.MARKET_DATA, payload)
            await self.state_hset(SK.MARKET.format(ticker), "snapshot", payload)
        except Exception as exc:
            self.log.warning("quote_handler_error", error=str(exc))

    async def _on_trade(self, trade) -> None:
        try:
            ticker = str(trade.symbol).upper()
            self._last[ticker]          = float(trade.price or 0.0)
            self._volume_delta[ticker] += int(trade.size or 0)
        except Exception as exc:
            self.log.warning("trade_handler_error", error=str(exc))

    # ── BaseAgent.run() ───────────────────────────────────────────────────────

    async def run(self) -> None:
        refresh_min = int(self._screener_cfg.get("refresh_minutes", 30))

        while self.running:
            # Run screener to get today's best candidates
            self.log.info("screener_running")
            self.watchlist = await self._build_watchlist()

            # Merge user-added tickers from Redis (added via UI)
            try:
                user_tickers_raw = await self.state_get(SK.WATCHLIST)
                if user_tickers_raw:
                    user_tickers = json.loads(user_tickers_raw) if isinstance(user_tickers_raw, str) else user_tickers_raw
                    for sym in user_tickers:
                        sym = str(sym).upper().strip()
                        if sym and sym not in self.watchlist:
                            self.watchlist.append(sym)
                    if user_tickers:
                        self.log.info("user_tickers_merged", added=[s for s in user_tickers if s.upper().strip() not in [w for w in self.watchlist[:len(self.watchlist)-len(user_tickers)]]])
            except Exception as exc:
                self.log.warning("user_watchlist_load_failed", error=str(exc))

            feed_str = os.getenv("ALPACA_DATA_FEED", "sip").lower()
            feed = DataFeed.SIP if feed_str == "sip" else DataFeed.IEX

            # Announce watchlist to the UI so Scanner pane shows what was picked
            source = "screener" if len(self.watchlist) >= 5 else "fallback"
            await self.publish(CH.MARKET_DATA, {
                "ticker":       "__WATCHLIST__",
                "timestamp_ms": int(time.time() * 1000),
                "bid": 0, "ask": 0, "spread": 0, "last": 0,
                "volume_delta": 0, "tape_anomaly": False,
                "sweep_detected": False,
                "unusual_size_bid": False, "unusual_size_ask": False,
                "_watchlist": self.watchlist,
                "_feed": feed_str,
                "_source": source,
            })

            self._stream = StockDataStream(
                api_key=os.getenv("ALPACA_API_KEY", ""),
                secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
                feed=feed,
            )
            self._stream.subscribe_quotes(self._on_quote, *self.watchlist)
            self._stream.subscribe_trades(self._on_trade, *self.watchlist)

            self.log.info("stream_connecting", tickers=self.watchlist,
                          feed=feed_str, refresh_in_min=refresh_min)

            async def _run_then_stop():
                await asyncio.to_thread(self._stream.run)

            stream_task = asyncio.create_task(_run_then_stop())

            await asyncio.sleep(refresh_min * 60)

            if self.running:
                self.log.info("screener_refresh", restarting=True)
                try:
                    self._stream.stop()
                except Exception:
                    pass
                await asyncio.gather(stream_task, return_exceptions=True)
            else:
                try:
                    self._stream.stop()
                except Exception:
                    pass
                break




# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    agent = MarketDataAgent()
    asyncio.run(agent.start())
