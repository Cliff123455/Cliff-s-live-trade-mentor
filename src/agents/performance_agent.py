# src/agents/performance_agent.py
# "The Historian" — logs completed trades, calculates per-setup statistics,
# and feeds performance weights back to The General via CH.PERFORMANCE_UPDATES.
# Does NOT make LLM calls.

import asyncio
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv()

from src.shared.base_agent import BaseAgent
from src.shared.constants import CH, SK, AGENTS


def _load_performance_config() -> dict:
    cfg_path = Path(__file__).resolve().parents[2] / "config" / "performance.yaml"
    with cfg_path.open() as f:
        return yaml.safe_load(f)


class PerformanceAgent(BaseAgent):
    """The Historian — trade logger and performance weight calculator."""

    def __init__(self):
        super().__init__(AGENTS.HISTORIAN)
        cfg = _load_performance_config()

        self._min_sample: int = int(cfg["min_sample_size"])
        self._degradation_threshold: float = float(cfg["degradation_threshold_pct"])
        self._degradation_window: int = int(cfg["degradation_window"])
        self._update_interval: float = float(cfg["update_interval_s"])

        # Open trades: directive_id -> partial trade record (entry received, no exit yet)
        self.open_trades: dict[str, dict] = {}

        # Buffered exits that arrived before the fill: directive_id -> exit msg
        self._buffered_exits: dict[str, dict] = {}

        # Completed trades list (all time, in memory)
        self.trades: list[dict] = []

        # Per-setup completed trade lists for analytics
        # setup_type -> list of completed trade dicts
        self._trades_by_setup: dict[str, list[dict]] = defaultdict(list)

        self._init_db()

    # ── SQLite persistence ────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create data/trades.db and ensure the trades table exists."""
        db_dir = Path(__file__).resolve().parents[2] / "data"
        db_dir.mkdir(exist_ok=True)
        self._db_path = str(db_dir / "trades.db")
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                directive_id TEXT UNIQUE,
                setup_id     TEXT,
                ticker       TEXT,
                setup_type   TEXT,
                direction    TEXT,
                entry_price  REAL,
                exit_price   REAL,
                shares       INTEGER,
                entry_ts     INTEGER,
                exit_ts      INTEGER,
                pnl          REAL,
                exit_reason  TEXT,
                recorded_at  TEXT
            )
        """)
        conn.commit()
        conn.close()
        self.log.info("trades_db_ready", path=self._db_path)

    def _write_trade_to_db(self, trade: dict) -> None:
        """Persist a completed trade row — synchronous, called via to_thread."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("""
                INSERT OR IGNORE INTO trades
                    (directive_id, setup_id, ticker, setup_type, direction,
                     entry_price, exit_price, shares, entry_ts, exit_ts,
                     pnl, exit_reason, recorded_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade.get("directive_id"),
                trade.get("setup_id"),
                trade.get("ticker"),
                trade.get("setup_type"),
                trade.get("direction"),
                trade.get("entry_price"),
                trade.get("exit_price"),
                trade.get("shares"),
                trade.get("entry_ts"),
                trade.get("exit_ts"),
                trade.get("pnl"),
                trade.get("exit_reason"),
                datetime.now(timezone.utc).isoformat(),
            ))
            conn.commit()
        finally:
            conn.close()

    # ── Config helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _calc_pnl(direction: str, entry_price: float, exit_price: float, shares: int) -> float:
        if direction == "short":
            return (entry_price - exit_price) * shares
        return (exit_price - entry_price) * shares

    # ── Weight calculation ────────────────────────────────────────────────────

    def _calc_weight(self, completed: list[dict]) -> Optional[float]:
        """
        Compute Kelly-inspired weight from completed trades.
        Returns None if sample is too small.
        Formula:
            win_rate = wins / total
            weight = (win_rate * avg_win) / ((1 - win_rate) * avg_loss)
            Clamped to [0.0, 1.0].
        """
        if len(completed) < self._min_sample:
            return None

        wins = [t["pnl"] for t in completed if t["pnl"] > 0]
        losses = [t["pnl"] for t in completed if t["pnl"] <= 0]

        if not wins or not losses:
            # Edge case: all wins => weight 1.0, all losses => weight 0.0
            return 1.0 if wins else 0.0

        win_rate = len(wins) / len(completed)
        avg_win = mean(wins)
        avg_loss = abs(mean(losses))

        if avg_loss == 0:
            return 1.0

        raw = (win_rate * avg_win) / ((1 - win_rate) * avg_loss)
        return max(0.0, min(1.0, raw))

    def _calc_degradation_flag(self, completed: list[dict]) -> bool:
        """
        True if rolling win_rate over the last `degradation_window` trades
        has dropped more than `degradation_threshold_pct` percentage points
        below the all-time win_rate for this setup_type.
        """
        total = len(completed)
        if total < self._min_sample:
            return False

        all_time_wr = sum(1 for t in completed if t["pnl"] > 0) / total

        window = completed[-self._degradation_window:]
        if len(window) < self._degradation_window:
            return False
        rolling_wr = sum(1 for t in window if t["pnl"] > 0) / len(window)

        drop_pct = (all_time_wr - rolling_wr) * 100.0
        return drop_pct > self._degradation_threshold

    # ── Performance payload builder ───────────────────────────────────────────

    def _build_performance_payload(self, setup_type: str) -> Optional[dict]:
        """
        Build the CH.PERFORMANCE_UPDATES payload for a given setup_type.
        Returns None if there are no completed trades for that setup_type.
        """
        completed = self._trades_by_setup.get(setup_type, [])
        if not completed:
            return None

        total = len(completed)
        wins = [t["pnl"] for t in completed if t["pnl"] > 0]
        losses = [t["pnl"] for t in completed if t["pnl"] <= 0]

        win_rate = len(wins) / total if total else 0.0
        avg_win_cents = round(mean(wins) * 100, 2) if wins else 0.0
        avg_loss_cents = round(abs(mean(losses)) * 100, 2) if losses else 0.0
        expectancy_cents = round(
            win_rate * avg_win_cents - (1 - win_rate) * avg_loss_cents, 2
        )

        weight = self._calc_weight(completed)
        degradation_flag = self._calc_degradation_flag(completed)

        return {
            "setup_type": setup_type,
            "sample_size": total,
            "win_rate": round(win_rate, 4),
            "avg_win_cents": avg_win_cents,
            "avg_loss_cents": avg_loss_cents,
            "expectancy_cents": expectancy_cents,
            "weight": round(weight, 4) if weight is not None else None,
            "degradation_flag": degradation_flag,
        }

    # ── Publish + persist helpers ──────────────────────────────────────────────

    async def _publish_and_persist_weights(self, setup_type: str) -> None:
        payload = self._build_performance_payload(setup_type)
        if payload is None:
            return

        # Persist weight to Redis hash so The General can reload on restart
        if payload["weight"] is not None:
            await self.state_hset(SK.SETUP_WEIGHTS, setup_type, payload["weight"])

        await self.publish(CH.PERFORMANCE_UPDATES, payload)

        self.log.info(
            "performance_update_published",
            setup_type=setup_type,
            sample_size=payload["sample_size"],
            win_rate=payload["win_rate"],
            weight=payload["weight"],
            degradation=payload["degradation_flag"],
        )

    async def _append_trade_log(self, trade: dict) -> None:
        """Write completed trade to Redis stream SK.TRADE_LOG."""
        try:
            await self.stream_append(SK.TRADE_LOG, trade)
        except Exception as exc:
            self.log.error("trade_log_append_failed", error=str(exc))

    # ── Message handlers ──────────────────────────────────────────────────────

    async def _handle_fill_report(self, msg: dict) -> None:
        """Record opening of a trade position."""
        directive_id = msg.get("directive_id")
        if not directive_id:
            self.log.warning("fill_report_missing_directive_id", msg=msg)
            return

        entry_price_raw = msg.get("fill_price") or msg.get("avg_fill_price") or msg.get("entry_price") or 0.0
        shares_raw = msg.get("shares") or msg.get("filled_shares") or msg.get("approved_shares") or 0

        record = {
            "directive_id": directive_id,
            "setup_id": msg.get("setup_id"),
            "ticker": msg.get("ticker"),
            "setup_type": msg.get("setup_type", "unknown"),
            "direction": msg.get("direction", msg.get("side", "long")),
            "entry_price": float(entry_price_raw),
            "shares": int(shares_raw),
            "entry_ts": msg.get("timestamp_ms", int(time.time() * 1000)),
            "exit_price": None,
            "exit_ts": None,
            "pnl": None,
        }

        self.open_trades[directive_id] = record
        self.log.info(
            "fill_recorded",
            directive_id=directive_id,
            ticker=record["ticker"],
            direction=record["direction"],
            entry_price=record["entry_price"],
            shares=record["shares"],
        )

        # Check if an exit arrived before the fill (race condition buffer)
        if directive_id in self._buffered_exits:
            self.log.info("processing_buffered_exit", directive_id=directive_id)
            buffered = self._buffered_exits.pop(directive_id)
            await self._handle_exit_order(buffered)

    async def _handle_exit_order(self, msg: dict) -> None:
        """Complete a trade record with P&L when position is closed."""
        directive_id = msg.get("directive_id")
        if not directive_id:
            self.log.warning("exit_order_missing_directive_id", msg=msg)
            return

        if directive_id not in self.open_trades:
            # Exit arrived before fill — buffer it
            self.log.info("exit_before_fill_buffered", directive_id=directive_id)
            self._buffered_exits[directive_id] = msg
            return

        record = self.open_trades.pop(directive_id)
        exit_price_raw = msg.get("fill_price") or msg.get("avg_fill_price") or msg.get("exit_price") or msg.get("exit_price_approx") or 0.0
        exit_price = float(exit_price_raw)
        exit_ts = msg.get("timestamp_ms", int(time.time() * 1000))

        pnl = self._calc_pnl(
            direction=record["direction"],
            entry_price=record["entry_price"],
            exit_price=exit_price,
            shares=record["shares"],
        )

        record["exit_price"] = exit_price
        record["exit_ts"] = exit_ts
        record["pnl"] = round(pnl, 4)
        record["exit_reason"] = msg.get("reason", "unknown")

        # Store completed trade
        self.trades.append(record)
        setup_type = record["setup_type"]
        self._trades_by_setup[setup_type].append(record)

        self.log.info(
            "trade_completed",
            directive_id=directive_id,
            ticker=record["ticker"],
            pnl=record["pnl"],
            setup_type=setup_type,
            total_trades=len(self.trades),
        )

        # ── Persist and publish ───────────────────────────────────────────────
        await self._append_trade_log(record)
        await asyncio.to_thread(lambda: self._write_trade_to_db(record))  # type: ignore[arg-type]
        await self.state_incrbyfloat(SK.DAILY_PNL, record["pnl"])
        await self._publish_and_persist_weights(setup_type)

    # ── Periodic update loop ──────────────────────────────────────────────────

    async def _periodic_update_loop(self) -> None:
        """Emit performance updates for all tracked setup types on a timer."""
        while self.running:
            await asyncio.sleep(self._update_interval)
            if not self.running:
                break
            if not self._trades_by_setup:
                continue
            for setup_type in list(self._trades_by_setup.keys()):
                try:
                    await self._publish_and_persist_weights(setup_type)
                except Exception as exc:
                    self.log.error(
                        "periodic_update_failed", setup_type=setup_type, error=str(exc)
                    )

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        await self.subscribe(CH.FILL_REPORTS, CH.EXIT_ORDERS)

        self.log.info(
            "historian_ready",
            min_sample=self._min_sample,
            update_interval=self._update_interval,
            degradation_threshold=self._degradation_threshold,
        )

        # Run the periodic broadcast alongside the message listener
        asyncio.create_task(self._periodic_update_loop())

        async for msg in self.listen():
            if not self.running:
                break

            msg_type = _classify_message(msg)

            if msg_type == "fill_report":
                await self._handle_fill_report(msg)
            elif msg_type == "exit_order":
                await self._handle_exit_order(msg)
            else:
                self.log.debug("unrouted_message", keys=list(msg.keys()))


def _classify_message(msg: dict) -> str:
    """Heuristically classify an incoming pub/sub message by its payload keys."""
    # Sniper tags every fill with is_exit — most reliable signal
    if "is_exit" in msg:
        return "exit_order" if msg["is_exit"] else "fill_report"
    # Fill reports come from the execution layer and confirm an entry fill
    if msg.get("fill_price") is not None or msg.get("fill_type") == "entry":
        return "fill_report"
    # Exit orders carry exit confirmation
    if msg.get("exit_type") is not None or msg.get("exit_price") is not None or msg.get("exit_price_approx") is not None:
        if "directive_id" in msg:
            return "exit_order"
    if msg.get("reason") and "directive_id" in msg:
        return "exit_order"
    # Fallback using agent origin hints
    if msg.get("agent") in ("sniper",) and "directive_id" in msg:
        return "fill_report"
    if msg.get("agent") in ("watcher",) and "directive_id" in msg:
        return "exit_order"
    return "unknown"


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    agent = PerformanceAgent()
    asyncio.run(agent.start())
