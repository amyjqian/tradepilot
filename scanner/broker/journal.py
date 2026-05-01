"""SQLite-backed trade journal + daily-session bookkeeping.

Stores three things:

  - `fills`: every commission-confirmed execution from IBKR. Keyed by IB's
    `execId` so re-running the same broker session is idempotent.
  - `trades`: closed round-trips, derived FIFO when a closing fill arrives
    that consumes opposing fill quantity. Each closed trade carries the
    qty-weighted entry price, the qty-weighted score-at-entry / planned
    stop, and the resulting R-multiple, P&L, and holding time.
  - `sessions`: one row per ET trading date. Tracks `start_equity` (the
    first NetLiquidation value observed that day) so the circuit breaker
    has a stable baseline across restarts. Persists `kill_active` so a
    tripped kill survives a process bounce.

Threading: every public method opens its own short-lived sqlite3
connection. A module-level lock guards `record_fill` (the only writer
that does multi-statement work) so FIFO matching is atomic. SQLite
itself uses WAL so concurrent readers don't block writers.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  exec_id TEXT UNIQUE NOT NULL,
  ts TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  qty REAL NOT NULL,
  price REAL NOT NULL,
  commission REAL NOT NULL DEFAULT 0,
  order_perm_id TEXT,
  score_at_entry REAL,
  planned_stop REAL,
  remaining_qty REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fills_pair ON fills(symbol, side, ts);

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  closed_at TEXT NOT NULL,
  qty REAL NOT NULL,
  entry_avg REAL NOT NULL,
  exit_avg REAL NOT NULL,
  planned_stop REAL,
  score_at_entry REAL,
  r_multiple REAL,
  pnl_abs REAL NOT NULL,
  pnl_pct REAL NOT NULL,
  holding_sec INTEGER NOT NULL,
  win INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at DESC);

-- Sessions key: (date_et, account). Earlier versions used (date_et) only,
-- which broke when switching between paper accounts mid-day — the
-- baseline from one account got compared against another's equity and
-- tripped the kill switch spuriously. The migration in
-- `_migrate_sessions_schema` drops + recreates this table on the old
-- shape since session rows are ephemeral per-day operational state.
CREATE TABLE IF NOT EXISTS sessions (
  date_et TEXT NOT NULL,
  account TEXT NOT NULL DEFAULT '',
  start_equity REAL NOT NULL,
  kill_active INTEGER NOT NULL DEFAULT 0,
  kill_tripped_at TEXT,
  kill_reason TEXT,
  PRIMARY KEY (date_et, account)
);
"""


def today_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


class Journal:
    """Thread-safe sqlite journal. One instance per process."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._connect() as conn:
            self._migrate_sessions_schema(conn)
            conn.executescript(_SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")

    @staticmethod
    def _migrate_sessions_schema(conn: sqlite3.Connection) -> None:
        """Drop the old `sessions` table if it lacks the `account` column.

        Old shape: PRIMARY KEY (date_et). New shape: PRIMARY KEY
        (date_et, account). Sessions are per-day operational state — no
        historical value — so we drop rather than back-fill on schema
        change.
        """
        info = conn.execute("PRAGMA table_info(sessions)").fetchall()
        if not info:
            return  # table doesn't exist yet
        cols = {row[1] for row in info}
        if "account" not in cols:
            log.info("Migrating sessions table: dropping pre-multi-account shape")
            conn.execute("DROP TABLE sessions")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Sessions / kill switch
    # ------------------------------------------------------------------

    def get_or_init_session(
        self, date_et: str, current_equity: float, account: str = ""
    ) -> dict[str, Any]:
        """Return the (`date_et`, `account`) session row, creating it
        with `start_equity = current_equity` if missing. Idempotent —
        the first caller of the day for that account defines the
        baseline; later callers just read it.
        """
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE date_et = ? AND account = ?",
                (date_et, account),
            ).fetchone()
            if row is not None:
                return dict(row)
            conn.execute(
                "INSERT INTO sessions (date_et, account, start_equity) VALUES (?, ?, ?)",
                (date_et, account, current_equity),
            )
            return {
                "date_et": date_et,
                "account": account,
                "start_equity": current_equity,
                "kill_active": 0,
                "kill_tripped_at": None,
                "kill_reason": None,
            }

    def get_session(self, date_et: str, account: str = "") -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE date_et = ? AND account = ?",
                (date_et, account),
            ).fetchone()
            return _row_to_dict(row)

    def trip_kill(self, date_et: str, reason: str, account: str = "") -> None:
        ts = datetime.utcnow().isoformat() + "Z"
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """UPDATE sessions
                   SET kill_active = 1, kill_tripped_at = ?, kill_reason = ?
                   WHERE date_et = ? AND account = ?""",
                (ts, reason, date_et, account),
            )

    def reseed_session_start(
        self, date_et: str, start_equity: float, account: str = ""
    ) -> None:
        """Overwrite a stale `start_equity` (e.g. one persisted before
        NetLiquidation actually arrived). Idempotent — only writes if
        the existing row has a non-positive start.
        """
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """UPDATE sessions
                   SET start_equity = ?
                   WHERE date_et = ? AND account = ? AND start_equity <= 0""",
                (start_equity, date_et, account),
            )

    def reset_kill(self, date_et: str, account: str = "") -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """UPDATE sessions
                   SET kill_active = 0, kill_tripped_at = NULL, kill_reason = NULL
                   WHERE date_et = ? AND account = ?""",
                (date_et, account),
            )

    # ------------------------------------------------------------------
    # Fills + FIFO trade pairing
    # ------------------------------------------------------------------

    def record_fill(
        self,
        *,
        exec_id: str,
        ts: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        commission: float = 0.0,
        order_perm_id: str | None = None,
        score_at_entry: float | None = None,
        planned_stop: float | None = None,
    ) -> dict[str, Any] | None:
        """Insert a fill (idempotent on `exec_id`) and pair it FIFO against
        any opposing remaining fills for the same symbol. Returns the
        closed trade dict if the fill triggered a close, else None.
        """
        side = side.lower()
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        if qty <= 0:
            raise ValueError("qty must be > 0")

        with self._write_lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM fills WHERE exec_id = ?", (exec_id,)
            ).fetchone()
            if existing is not None:
                return None  # idempotent — same fill from a duplicate event
            conn.execute(
                """INSERT INTO fills
                   (exec_id, ts, symbol, side, qty, price, commission,
                    order_perm_id, score_at_entry, planned_stop, remaining_qty)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    exec_id, ts, symbol, side, qty, price, commission,
                    order_perm_id, score_at_entry, planned_stop, qty,
                ),
            )
            return self._pair_fifo(conn, symbol=symbol, closing_side=side)

    def _pair_fifo(
        self, conn: sqlite3.Connection, *, symbol: str, closing_side: str
    ) -> dict[str, Any] | None:
        """If the just-inserted fill on `closing_side` opposes existing
        unmatched fills for this symbol, consume them oldest-first to emit
        a closed trade. Each call emits at most one trade row (per closing
        fill); partial closes leave residual `remaining_qty` for later.
        """
        opposite = "sell" if closing_side == "buy" else "buy"
        # Pick up the most recent closing fill that still has remaining.
        closing = conn.execute(
            """SELECT * FROM fills
               WHERE symbol = ? AND side = ? AND remaining_qty > 0
               ORDER BY ts DESC, id DESC LIMIT 1""",
            (symbol, closing_side),
        ).fetchone()
        if closing is None:
            return None

        # Opposing entries oldest-first.
        entries = conn.execute(
            """SELECT * FROM fills
               WHERE symbol = ? AND side = ? AND remaining_qty > 0
               ORDER BY ts ASC, id ASC""",
            (symbol, opposite),
        ).fetchall()
        if not entries:
            return None

        close_remaining = float(closing["remaining_qty"])
        consumed_qty = 0.0
        weighted_entry = 0.0
        weighted_stop_num = 0.0
        weighted_stop_den = 0.0
        weighted_score_num = 0.0
        weighted_score_den = 0.0
        opened_at: str | None = None

        for e in entries:
            if close_remaining <= 0:
                break
            avail = float(e["remaining_qty"])
            take = min(avail, close_remaining)
            weighted_entry += take * float(e["price"])
            if e["planned_stop"] is not None:
                weighted_stop_num += take * float(e["planned_stop"])
                weighted_stop_den += take
            if e["score_at_entry"] is not None:
                weighted_score_num += take * float(e["score_at_entry"])
                weighted_score_den += take
            if opened_at is None:
                opened_at = str(e["ts"])
            new_rem = avail - take
            conn.execute(
                "UPDATE fills SET remaining_qty = ? WHERE id = ?",
                (new_rem, int(e["id"])),
            )
            consumed_qty += take
            close_remaining -= take

        # Update the closing fill's remaining.
        conn.execute(
            "UPDATE fills SET remaining_qty = ? WHERE id = ?",
            (close_remaining, int(closing["id"])),
        )

        if consumed_qty <= 0:
            return None

        entry_avg = weighted_entry / consumed_qty
        exit_avg = float(closing["price"])
        planned_stop = (
            weighted_stop_num / weighted_stop_den if weighted_stop_den > 0 else None
        )
        score = (
            weighted_score_num / weighted_score_den if weighted_score_den > 0 else None
        )
        # Position direction: if entries were buys, this is a long close;
        # if entries were sells, it's a short close.
        side_label = "long" if opposite == "buy" else "short"
        if side_label == "long":
            pnl_abs = (exit_avg - entry_avg) * consumed_qty
            r_mult = (
                (exit_avg - entry_avg) / (entry_avg - planned_stop)
                if planned_stop is not None and (entry_avg - planned_stop) > 0
                else None
            )
        else:
            pnl_abs = (entry_avg - exit_avg) * consumed_qty
            r_mult = (
                (entry_avg - exit_avg) / (planned_stop - entry_avg)
                if planned_stop is not None and (planned_stop - entry_avg) > 0
                else None
            )
        pnl_pct = (
            (pnl_abs / (entry_avg * consumed_qty)) * 100.0 if entry_avg > 0 else 0.0
        )
        closed_at = str(closing["ts"])
        holding_sec = _holding_seconds(opened_at or closed_at, closed_at)
        win = 1 if pnl_abs > 0 else 0

        cur = conn.execute(
            """INSERT INTO trades
               (symbol, side, opened_at, closed_at, qty, entry_avg, exit_avg,
                planned_stop, score_at_entry, r_multiple, pnl_abs, pnl_pct,
                holding_sec, win)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                symbol, side_label, opened_at or closed_at, closed_at,
                consumed_qty, entry_avg, exit_avg, planned_stop, score,
                r_mult, pnl_abs, pnl_pct, holding_sec, win,
            ),
        )
        trade_id = cur.lastrowid
        return {
            "id": trade_id,
            "symbol": symbol,
            "side": side_label,
            "opened_at": opened_at or closed_at,
            "closed_at": closed_at,
            "qty": consumed_qty,
            "entry_avg": entry_avg,
            "exit_avg": exit_avg,
            "planned_stop": planned_stop,
            "score_at_entry": score,
            "r_multiple": r_mult,
            "pnl_abs": pnl_abs,
            "pnl_pct": pnl_pct,
            "holding_sec": holding_sec,
            "win": win,
        }

    # ------------------------------------------------------------------
    # Read API for the dashboard
    # ------------------------------------------------------------------

    def list_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY closed_at DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
            return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        """Aggregate stats across all closed trades. Cheap enough to compute
        on demand — these tables are tiny.
        """
        with self._connect() as conn:
            row = conn.execute(
                """SELECT
                     COUNT(*)              AS n,
                     COALESCE(SUM(win), 0) AS wins,
                     COALESCE(AVG(r_multiple), 0) AS avg_r,
                     COALESCE(AVG(pnl_pct), 0)    AS avg_pnl_pct,
                     COALESCE(SUM(pnl_abs), 0)    AS total_pnl,
                     COALESCE(AVG(holding_sec), 0) AS avg_hold
                   FROM trades"""
            ).fetchone()
        n = int(row["n"] or 0)
        wins = int(row["wins"] or 0)
        return {
            "n_trades": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate_pct": (wins / n * 100.0) if n else 0.0,
            "avg_r": float(row["avg_r"] or 0.0),
            "avg_pnl_pct": float(row["avg_pnl_pct"] or 0.0),
            "total_pnl_abs": float(row["total_pnl"] or 0.0),
            "avg_hold_sec": float(row["avg_hold"] or 0.0),
        }


def _holding_seconds(opened_at: str, closed_at: str) -> int:
    try:
        a = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        b = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        return max(0, int((b - a).total_seconds()))
    except Exception:
        return 0
