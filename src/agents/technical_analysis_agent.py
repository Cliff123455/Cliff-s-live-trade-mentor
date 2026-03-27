# src/agents/technical_analysis_agent.py
# "The Chartist" — Consumes live market data, computes indicators, detects
# scalping setups, scores them, and publishes qualified setups to CH.SETUPS.

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import pandas_ta as ta
import yaml
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from src.shared.base_agent import BaseAgent
from src.shared.constants import AGENTS, CH, SK, SETUP

load_dotenv()

# ── Config loader ─────────────────────────────────────────────────────────────

def _load_config() -> dict:
    with open("config/indicators.yaml", "r") as fh:
        return yaml.safe_load(fh)


# ── Agent ─────────────────────────────────────────────────────────────────────

class TechnicalAnalysisAgent(BaseAgent):
    """The Chartist — detects and scores technical setups from 1-min bar data."""

    def __init__(self):
        super().__init__(AGENTS.CHARTIST)
        cfg = _load_config()

        # Indicator parameters
        self.ema_periods: list[int]      = cfg.get("ema_periods", [5, 9, 21])
        self.rsi_period: int             = cfg.get("rsi_period", 14)
        self.rsi_oversold: int           = cfg.get("rsi_oversold", 35)
        self.rsi_overbought: int         = cfg.get("rsi_overbought", 65)
        self.atr_period: int             = cfg.get("atr_period", 14)
        self.vwap_enabled: bool          = cfg.get("vwap_enabled", True)
        self.vol_confirm_mult: float     = cfg.get("volume_confirmation_multiplier", 1.5)
        self.min_publish_quality: int    = cfg.get("min_publish_quality", 5)
        self.watchlist: list[str]        = [t.upper() for t in cfg.get("watchlist", [])]
        self.bar_timeframe: str          = cfg.get("bar_timeframe", "1Min")
        self.history_bars: int           = cfg.get("history_bars", 60)

        # Watchlist: accept ANY ticker the Scanner sends (dynamic screener)
        # Optionally seed from fallback_watchlist so history load has something to work with
        seed = cfg.get("fallback_watchlist", cfg.get("watchlist", []))
        self.watchlist: set[str]         = {t.upper() for t in seed}
        self._dynamic_watchlist: bool    = True   # accept tickers not in seed list

        # ATR stop multiplier (mirrors risk-params for target calculation)
        self._atr_stop_mult: float       = 1.5
        self._min_rr_ratio: float        = 2.0

        # Rolling OHLCV DataFrames keyed by ticker  (max self.history_bars rows)
        self.bars: dict[str, pd.DataFrame] = {}

        # Session high-of-day per ticker
        self._session_high: dict[str, float] = {}

        # Duplicate-setup suppression: (ticker, setup_type) -> last publish timestamp_ms
        self._last_published: dict[tuple[str, str], int] = {}
        self._cooldown_ms: int = 60_000   # 60 seconds

        # Diagnostic snapshot counter per ticker (publish snapshot every N packets)
        self._pkt_count: dict[str, int] = {}
        self._snapshot_every: int = 200   # emit indicator summary every 200 packets

        # Throttle: only recompute indicators at most once per second per ticker
        self._last_compute_ts: dict[str, float] = {}
        self._compute_interval_s: float = 1.0

    # ── Startup history load ──────────────────────────────────────────────────

    async def _load_history(self) -> None:
        """Fetch the last N 1-min bars for all known watchlist tickers from Alpaca."""
        api_key    = os.getenv("ALPACA_API_KEY", "")
        secret_key = os.getenv("ALPACA_SECRET_KEY", "")

        if not api_key or not secret_key:
            self.log.warning("alpaca_creds_missing", msg="History load skipped")
            return

        symbols = list(self.watchlist)
        if not symbols:
            self.log.info("history_skip", reason="watchlist empty at startup — will load on first packet")
            return

        from datetime import timedelta
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=3)

        client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)

        request = StockBarsRequest(
            symbol_or_symbols=list(self.watchlist),
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            limit=self.history_bars * 2,
        )

        try:
            bars_response = client.get_stock_bars(request)

        except Exception as exc:
            self.log.error("history_load_failed", error=str(exc))
            return

        for ticker in self.watchlist:
            try:
                df = bars_response.df
                if hasattr(df.index, "levels"):
                    # Multi-index: (symbol, timestamp)
                    if ticker in df.index.get_level_values(0):
                        df = df.xs(ticker, level=0).copy()
                    else:
                        self.log.warning("no_history", ticker=ticker)
                        continue
                else:
                    df = df[df["symbol"] == ticker].copy() if "symbol" in df.columns else df.copy()

                df = df.rename(columns={
                    "open":   "open",
                    "high":   "high",
                    "low":    "low",
                    "close":  "close",
                    "volume": "volume",
                    "vwap":   "alpaca_vwap",   # keep separate; we compute our own
                })

                # Ensure correct dtypes and sort
                for col in ["open", "high", "low", "close", "volume"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.sort_index().tail(self.history_bars).reset_index()

                self.bars[ticker] = df
                self._session_high[ticker] = float(df["high"].max()) if not df.empty else 0.0
                self.log.info("history_loaded", ticker=ticker, bars=len(df))

            except Exception as exc:
                self.log.warning("history_parse_error", ticker=ticker, error=str(exc))

    # ── Market data ingestion ─────────────────────────────────────────────────

    def _ingest_packet(self, packet: dict) -> Optional[pd.DataFrame]:
        """
        Accept any ticker from The Scanner (dynamic watchlist).
        Build/update a rolling OHLCV DataFrame from quote ticks.
        """
        ticker = packet.get("ticker", "").upper()
        if not ticker:
            return None

        # Auto-add any new ticker the Scanner sends
        self.watchlist.add(ticker)

        last      = float(packet.get("last", 0.0))
        bid       = float(packet.get("bid", last))
        ask       = float(packet.get("ask", last))
        volume    = int(packet.get("volume_delta", 0))
        ts_ms     = int(packet.get("timestamp_ms", int(time.time() * 1000)))

        if last <= 0:
            return None

        mid = (bid + ask) / 2.0 if ask > 0 and bid > 0 else last

        new_row = pd.DataFrame([{
            "timestamp": pd.Timestamp(ts_ms, unit="ms", tz="UTC"),
            "open":   mid,
            "high":   mid,
            "low":    mid,
            "close":  mid,
            "volume": volume,
        }])

        if ticker not in self.bars or self.bars[ticker].empty:
            self.bars[ticker] = new_row
        else:
            df = self.bars[ticker]
            # Append and keep only the last history_bars rows
            df = pd.concat([df, new_row], ignore_index=True).tail(self.history_bars)
            self.bars[ticker] = df

        # Track session high
        current_high = float(self.bars[ticker]["high"].max())
        self._session_high[ticker] = max(self._session_high.get(ticker, 0.0), current_high)

        return self.bars[ticker].copy()

    # ── Indicator computation ─────────────────────────────────────────────────

    def _compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Appends EMA, RSI, ATR, and VWAP columns in-place and returns df."""
        if len(df) < max(self.ema_periods + [self.rsi_period, self.atr_period]):
            return df

        # EMAs
        for period in self.ema_periods:
            col = f"EMA_{period}"
            result = df.ta.ema(length=period, append=False)
            if result is not None:
                df[col] = result.values

        # RSI
        rsi_result = df.ta.rsi(length=self.rsi_period, append=False)
        if rsi_result is not None:
            df["RSI"] = rsi_result.values

        # ATR
        atr_result = df.ta.atr(length=self.atr_period, append=False)
        if atr_result is not None:
            # pandas_ta names the ATR column with a prefix
            df["ATR"] = atr_result.values

        # VWAP (session-reset: cumulative from first bar)
        if self.vwap_enabled and "volume" in df.columns:
            typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
            pv = typical_price * df["volume"]
            cum_vol = df["volume"].cumsum()
            df["VWAP"] = pv.cumsum() / cum_vol.where(cum_vol > 0, other=1)

        return df

    # ── Volume confirmation ───────────────────────────────────────────────────

    def _volume_confirmed(self, df: pd.DataFrame) -> bool:
        """True if the last bar's volume exceeds vol_confirm_mult * rolling average."""
        if len(df) < 5:
            return False
        avg_vol = float(df["volume"].iloc[:-1].mean())
        last_vol = float(df["volume"].iloc[-1])
        if avg_vol <= 0:
            return False
        return last_vol >= avg_vol * self.vol_confirm_mult

    # ── Setup detectors ───────────────────────────────────────────────────────

    def _detect_vwap_reclaim(self, df: pd.DataFrame, ticker: str) -> Optional[dict]:
        """
        VWAP_RECLAIM: price was below VWAP on the prior bar and is now above it.
        RSI < 50, volume confirmation required.
        """
        if "VWAP" not in df.columns or len(df) < 3:
            return None

        prev_close = float(df["close"].iloc[-2])
        curr_close = float(df["close"].iloc[-1])
        prev_vwap  = float(df["VWAP"].iloc[-2])
        curr_vwap  = float(df["VWAP"].iloc[-1])
        rsi_val    = float(df["RSI"].iloc[-1]) if "RSI" in df.columns and not pd.isna(df["RSI"].iloc[-1]) else 50.0
        atr        = float(df["ATR"].iloc[-1]) if "ATR" in df.columns and not pd.isna(df["ATR"].iloc[-1]) else 0.0

        # Cross: was below, now above
        crossed_above = (prev_close < prev_vwap) and (curr_close > curr_vwap)
        if not crossed_above:
            return None
        if rsi_val >= 50:
            return None
        if not self._volume_confirmed(df):
            return None
        if atr <= 0:
            return None

        # Scoring: base 6, +1 for RSI < 40, +1 for strong volume
        score = 6
        if rsi_val < 40:
            score += 1
        avg_vol = float(df["volume"].iloc[:-1].mean())
        last_vol = float(df["volume"].iloc[-1])
        if avg_vol > 0 and last_vol >= avg_vol * self.vol_confirm_mult * 1.5:
            score += 1
        score = min(score, 10)

        ema5_above  = "EMA_5"  in df.columns and not pd.isna(df["EMA_5"].iloc[-1])  and curr_close > df["EMA_5"].iloc[-1]
        ema9_above  = "EMA_9"  in df.columns and not pd.isna(df["EMA_9"].iloc[-1])  and curr_close > df["EMA_9"].iloc[-1]

        return {
            "setup_type": SETUP.VWAP_RECLAIM,
            "direction":  "long",
            "quality_score": score,
            "atr": atr,
            "curr_vwap": curr_vwap,
            "signals": {
                "vwap": "reclaim_confirmed",
                "ema_5": "above" if ema5_above else "below",
                "ema_9": "above" if ema9_above else "below",
                "rsi": round(rsi_val, 1),
                "volume_confirmation": True,
            },
        }

    def _detect_ema_bounce(self, df: pd.DataFrame, ticker: str) -> Optional[dict]:
        """
        EMA_BOUNCE: price touched the 9 EMA (low <= EMA_9) and closed above it,
        with EMAs in long order (EMA_5 > EMA_9 > EMA_21).
        Also handles short (EMA_5 < EMA_9 < EMA_21, high >= EMA_9, close below).
        """
        needed = ["EMA_5", "EMA_9", "EMA_21", "RSI", "ATR"]
        for col in needed:
            if col not in df.columns or len(df) < 3:
                return None
            if pd.isna(df[col].iloc[-1]):
                return None

        ema5  = float(df["EMA_5"].iloc[-1])
        ema9  = float(df["EMA_9"].iloc[-1])
        ema21 = float(df["EMA_21"].iloc[-1])
        low   = float(df["low"].iloc[-1])
        high  = float(df["high"].iloc[-1])
        close = float(df["close"].iloc[-1])
        rsi   = float(df["RSI"].iloc[-1])
        atr   = float(df["ATR"].iloc[-1])

        # Long setup
        if ema5 > ema9 > ema21:
            touched_9  = low <= ema9 * 1.001   # within 0.1%
            bounced    = close > ema9
            if touched_9 and bounced:
                score = 6
                if ema5 > ema9 * 1.001:    # EMAs nicely spread
                    score += 1
                if rsi < 55:
                    score += 1
                score = min(score, 10)
                return {
                    "setup_type": SETUP.EMA_BOUNCE,
                    "direction":  "long",
                    "quality_score": score,
                    "atr": atr,
                    "signals": {
                        "ema_5": round(ema5, 4),
                        "ema_9": round(ema9, 4),
                        "ema_21": round(ema21, 4),
                        "rsi": round(rsi, 1),
                        "volume_confirmation": self._volume_confirmed(df),
                    },
                }

        # Short setup
        if ema5 < ema9 < ema21:
            touched_9  = high >= ema9 * 0.999
            bounced    = close < ema9
            if touched_9 and bounced:
                score = 6
                if ema5 < ema9 * 0.999:
                    score += 1
                if rsi > 45:
                    score += 1
                score = min(score, 10)
                return {
                    "setup_type": SETUP.EMA_BOUNCE,
                    "direction":  "short",
                    "quality_score": score,
                    "atr": atr,
                    "signals": {
                        "ema_5": round(ema5, 4),
                        "ema_9": round(ema9, 4),
                        "ema_21": round(ema21, 4),
                        "rsi": round(rsi, 1),
                        "volume_confirmation": self._volume_confirmed(df),
                    },
                }

        return None

    def _detect_hod_breakout(self, df: pd.DataFrame, ticker: str) -> Optional[dict]:
        """
        HOD_BREAKOUT: current close exceeds the prior session high with above-average volume.
        """
        if len(df) < 3:
            return None
        if "ATR" not in df.columns or pd.isna(df["ATR"].iloc[-1]):
            return None

        close         = float(df["close"].iloc[-1])
        session_high  = self._session_high.get(ticker, 0.0)
        atr           = float(df["ATR"].iloc[-1])
        rsi           = float(df["RSI"].iloc[-1]) if "RSI" in df.columns and not pd.isna(df["RSI"].iloc[-1]) else 50.0

        # Only count as breakout if price closed decisively above HOD
        if session_high <= 0 or close <= session_high:
            return None
        if not self._volume_confirmed(df):
            return None

        score = 7
        avg_vol  = float(df["volume"].iloc[:-1].mean())
        last_vol = float(df["volume"].iloc[-1])
        if avg_vol > 0 and last_vol >= avg_vol * self.vol_confirm_mult * 2.0:
            score += 1
        if rsi < 70:
            score += 1
        score = min(score, 10)

        return {
            "setup_type": SETUP.HOD_BREAKOUT,
            "direction":  "long",
            "quality_score": score,
            "atr": atr,
            "signals": {
                "prior_hod": round(session_high, 4),
                "breakout_close": round(close, 4),
                "rsi": round(rsi, 1),
                "volume_confirmation": True,
            },
        }

    def _detect_momentum_continuation(self, df: pd.DataFrame, ticker: str) -> Optional[dict]:
        """
        MOMENTUM_CONTINUATION: RSI 50–65, all EMAs aligned upward (EMA5 > EMA9 > EMA21),
        volume spike on the current bar.
        """
        needed = ["EMA_5", "EMA_9", "EMA_21", "RSI", "ATR"]
        for col in needed:
            if col not in df.columns or len(df) < 3:
                return None
            if pd.isna(df[col].iloc[-1]):
                return None

        ema5  = float(df["EMA_5"].iloc[-1])
        ema9  = float(df["EMA_9"].iloc[-1])
        ema21 = float(df["EMA_21"].iloc[-1])
        rsi   = float(df["RSI"].iloc[-1])
        atr   = float(df["ATR"].iloc[-1])

        aligned_long = ema5 > ema9 > ema21

        if not aligned_long:
            return None
        if not (50.0 <= rsi <= 65.0):
            return None
        if not self._volume_confirmed(df):
            return None

        score = 6
        spread = (ema5 - ema21) / ema21 if ema21 > 0 else 0
        if spread > 0.002:       # EMAs nicely fanned
            score += 1
        if rsi < 60:
            score += 1
        score = min(score, 10)

        return {
            "setup_type": SETUP.MOMENTUM_CONTINUATION,
            "direction":  "long",
            "quality_score": score,
            "atr": atr,
            "signals": {
                "ema_5":  round(ema5, 4),
                "ema_9":  round(ema9, 4),
                "ema_21": round(ema21, 4),
                "rsi":    round(rsi, 1),
                "volume_confirmation": True,
            },
        }

    # ── Setup builder (entry zone / stops / targets) ──────────────────────────

    def _build_setup_payload(
        self,
        ticker: str,
        packet: dict,
        setup: dict,
    ) -> Optional[dict]:
        """
        Construct the full CH.SETUPS payload from a raw detector result.
        """
        atr        = setup.get("atr", 0.0)
        direction  = setup.get("direction", "long")
        bid        = float(packet.get("bid", packet.get("last", 0.0)))
        ask        = float(packet.get("ask", packet.get("last", 0.0)))

        if ask <= 0 or atr <= 0:
            return None

        entry_mid = (bid + ask) / 2.0

        if direction == "long":
            suggested_stop   = round(entry_mid - (atr * self._atr_stop_mult), 4)
            suggested_target = round(entry_mid + (atr * self._atr_stop_mult * self._min_rr_ratio * 1.5), 4)
        else:
            suggested_stop   = round(entry_mid + (atr * self._atr_stop_mult), 4)
            suggested_target = round(entry_mid - (atr * self._atr_stop_mult * self._min_rr_ratio * 1.5), 4)

        return {
            "setup_id":        self.make_id(),
            "ticker":          ticker,
            "timestamp_ms":    int(time.time() * 1000),
            "setup_type":      setup["setup_type"],
            "direction":       direction,
            "quality_score":   setup["quality_score"],
            "entry_zone":      [round(bid, 4), round(ask, 4)],
            "suggested_stop":  suggested_stop,
            "suggested_target": suggested_target,
            "signals":         setup.get("signals", {}),
        }

    # ── Duplicate suppression ─────────────────────────────────────────────────

    def _is_duplicate(self, ticker: str, setup_type: str) -> bool:
        key     = (ticker, setup_type)
        last_ts = self._last_published.get(key, 0)
        return (int(time.time() * 1000) - last_ts) < self._cooldown_ms

    def _mark_published(self, ticker: str, setup_type: str) -> None:
        self._last_published[(ticker, setup_type)] = int(time.time() * 1000)

    # ── Per-packet analysis pipeline ──────────────────────────────────────────

    async def _process_packet(self, packet: dict) -> None:
        ticker = packet.get("ticker", "").upper()
        if not ticker:
            return

        df = self._ingest_packet(packet)
        has_enough = df is not None and len(df) >= max(self.ema_periods + [self.rsi_period, self.atr_period])

        if df is not None:
            self._pkt_count[ticker] = self._pkt_count.get(ticker, 0) + 1

        if not has_enough:
            if df is not None and self._pkt_count.get(ticker, 0) % self._snapshot_every == 0:
                await self.publish(CH.SETUPS, {
                    "setup_id":     self.make_id(),
                    "ticker":       ticker,
                    "timestamp_ms": int(time.time() * 1000),
                    "setup_type":   "warming_up",
                    "direction":    "none",
                    "quality_score": 0,
                    "entry_zone":   [0, 0],
                    "suggested_stop": 0,
                    "suggested_target": 0,
                    "signals": {
                        "bars_collected": len(df),
                        "bars_needed":    max(self.ema_periods + [self.rsi_period, self.atr_period]),
                        "status": "warming_up",
                    },
                })
            return

        # ── Throttle: only recompute indicators once per second per ticker ──
        now_ts = time.time()
        last_compute = self._last_compute_ts.get(ticker, 0.0)
        if now_ts - last_compute < self._compute_interval_s:
            return   # skip heavy computation — too soon
        self._last_compute_ts[ticker] = now_ts

        df = self._compute_indicators(df)

        # Periodic indicator snapshot
        if self._pkt_count.get(ticker, 0) % self._snapshot_every == 0:
            last_row = df.iloc[-1]
            def _safe(col): return round(float(last_row[col]), 4) if col in df.columns and not pd.isna(last_row.get(col, float('nan'))) else None
            await self.publish(CH.SETUPS, {
                "setup_id":     self.make_id(),
                "ticker":       ticker,
                "timestamp_ms": int(time.time() * 1000),
                "setup_type":   "indicator_snapshot",
                "direction":    "none",
                "quality_score": 0,
                "entry_zone":   [0, 0],
                "suggested_stop": 0,
                "suggested_target": 0,
                "signals": {
                    "ema_5":   _safe("EMA_5"),
                    "ema_9":   _safe("EMA_9"),
                    "ema_21":  _safe("EMA_21"),
                    "rsi":     _safe("RSI"),
                    "atr":     _safe("ATR"),
                    "vwap":    _safe("VWAP"),
                    "close":   round(float(last_row["close"]), 4),
                    "bars":    len(df),
                },
            })

        detectors = [
            self._detect_vwap_reclaim,
            self._detect_ema_bounce,
            self._detect_hod_breakout,
            self._detect_momentum_continuation,
        ]

        for detector in detectors:
            try:
                setup = detector(df, ticker)
            except Exception as exc:
                self.log.warning("detector_error", detector=detector.__name__, ticker=ticker, error=str(exc))
                continue

            if setup is None:
                continue
            if setup["quality_score"] < self.min_publish_quality:
                continue
            if self._is_duplicate(ticker, setup["setup_type"]):
                continue

            payload = self._build_setup_payload(ticker, packet, setup)
            if payload is None:
                continue

            await self.publish(CH.SETUPS, payload)
            self._mark_published(ticker, setup["setup_type"])

            self.log.info(
                "setup_published",
                ticker=ticker,
                setup_type=setup["setup_type"],
                direction=setup["direction"],
                quality=setup["quality_score"],
                setup_id=payload["setup_id"],
            )

    # ── BaseAgent.run() ───────────────────────────────────────────────────────

    async def run(self) -> None:
        self.log.info("chartist_starting", watchlist=self.watchlist)

        await self._load_history()

        await self.subscribe(CH.MARKET_DATA)
        self.log.info("subscribed", channels=[CH.MARKET_DATA])

        async for message in self.listen():
            if not self.running:
                break
            try:
                await self._process_packet(message)
            except Exception as exc:
                self.log.error("packet_processing_error", error=str(exc))


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    agent = TechnicalAnalysisAgent()
    asyncio.run(agent.start())
