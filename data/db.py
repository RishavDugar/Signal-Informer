"""
Database layer — SQLite with WAL journal mode.

Safety guarantees:
  - WAL mode:            readers never block writers; crash-safe
  - synchronous=NORMAL:  durable without fsync overhead
  - foreign_keys=ON:     referential integrity enforced
  - Every stock write is a single transaction — all-or-nothing
  - UNIQUE constraints prevent duplicate dates per stock
  - integrity_check() exposes validation for callers
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Generator

import pandas as pd

from config import DB_PATH
from utils.logger import get_logger

log = get_logger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA cache_size=-65536;

CREATE TABLE IF NOT EXISTS stocks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    exchange    TEXT    NOT NULL DEFAULT 'NSE',
    name        TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1,
    last_updated TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
    , UNIQUE(symbol)
);

CREATE TABLE IF NOT EXISTS ohlcv (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_id INTEGER NOT NULL REFERENCES stocks(id) ON DELETE CASCADE,
    date     TEXT    NOT NULL,
    open     REAL    NOT NULL,
    high     REAL    NOT NULL,
    low      REAL    NOT NULL,
    close    REAL    NOT NULL,
    volume   INTEGER NOT NULL,
    UNIQUE(stock_id, date)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_stock_date ON ohlcv(stock_id, date DESC);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at         TEXT NOT NULL,
    status         TEXT NOT NULL,
    total_stocks   INTEGER,
    successful     INTEGER,
    failed         INTEGER,
    failed_symbols TEXT,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS setup_signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_id   INTEGER NOT NULL REFERENCES stocks(id) ON DELETE CASCADE,
    setup_name TEXT    NOT NULL,
    signal_date TEXT   NOT NULL,
    signal     INTEGER NOT NULL,
    metadata   TEXT,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(stock_id, setup_name, signal_date)
);

CREATE INDEX IF NOT EXISTS idx_signals_date  ON setup_signals(signal_date DESC);
CREATE INDEX IF NOT EXISTS idx_signals_stock ON setup_signals(stock_id, signal_date DESC);

CREATE TABLE IF NOT EXISTS adjustment_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_id    INTEGER NOT NULL REFERENCES stocks(id),
    detected_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    action_type TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS news_recommendations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT    NOT NULL,
    company_name   TEXT    NOT NULL DEFAULT '',
    rec_date       TEXT    NOT NULL DEFAULT (date('now')),
    catalyst       TEXT    NOT NULL DEFAULT '',
    news_snippets  TEXT,
    analysis       TEXT    NOT NULL DEFAULT '',
    cmp            REAL,
    change_1d_pct  REAL,
    change_5d_pct  REAL,
    change_20d_pct REAL,
    whatsapp_sent  INTEGER NOT NULL DEFAULT 0,
    sent_at        TEXT,
    expires_at     TEXT    NOT NULL,
    UNIQUE(symbol, rec_date)
);

CREATE INDEX IF NOT EXISTS idx_news_rec_date   ON news_recommendations(rec_date DESC);
CREATE INDEX IF NOT EXISTS idx_news_rec_symbol ON news_recommendations(symbol, rec_date DESC);

CREATE TABLE IF NOT EXISTS scout_recommendations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    scout_type     TEXT    NOT NULL,                  -- hidden_gems | small_cap_growth | smart_money
    symbol         TEXT    NOT NULL,
    company_name   TEXT    NOT NULL DEFAULT '',
    rec_date       TEXT    NOT NULL DEFAULT (date('now')),
    catalyst       TEXT    NOT NULL DEFAULT '',
    reasoning      TEXT    NOT NULL DEFAULT '',
    analysis       TEXT    NOT NULL DEFAULT '',
    cmp            REAL,
    change_1d_pct  REAL,
    change_5d_pct  REAL,
    change_20d_pct REAL,
    whatsapp_sent  INTEGER NOT NULL DEFAULT 0,
    sent_at        TEXT,
    expires_at     TEXT    NOT NULL,
    UNIQUE(scout_type, symbol, rec_date)
);

CREATE INDEX IF NOT EXISTS idx_scout_rec_date   ON scout_recommendations(scout_type, rec_date DESC);
CREATE INDEX IF NOT EXISTS idx_scout_rec_symbol ON scout_recommendations(scout_type, symbol, rec_date DESC);
"""


# ── Connection factory ────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    # Apply PRAGMAs on every new connection
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=-65536")
    return conn


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


# ── Schema initialisation ─────────────────────────────────────────────────────

def init_db() -> None:
    """Create all tables if they don't exist. Safe to call multiple times."""
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        conn.commit()
    log.info("db: schema initialised")


def integrity_check() -> bool:
    """Run SQLite integrity_check. Returns True if database is healthy."""
    try:
        with get_conn() as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        ok = result and result[0] == "ok"
        if not ok:
            log.error(f"db: integrity_check FAILED: {result[0] if result else 'no result'}")
        return ok
    except Exception as exc:
        log.error(f"db: integrity_check error — {exc}")
        return False


# ── Stock registry ────────────────────────────────────────────────────────────

def upsert_stock(symbol: str, exchange: str = "NSE", name: str | None = None) -> int:
    """Insert or ignore stock; returns its id."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO stocks (symbol, exchange, name)
            VALUES (?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                exchange = excluded.exchange,
                name     = COALESCE(excluded.name, stocks.name)
            """,
            (symbol, exchange, name),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM stocks WHERE symbol = ?", (symbol,)).fetchone()
    return row["id"]


def get_stock_id(symbol: str) -> int | None:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM stocks WHERE symbol = ?", (symbol,)).fetchone()
    return row["id"] if row else None


def get_active_stocks() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, symbol, exchange, name FROM stocks WHERE is_active = 1 ORDER BY symbol"
        ).fetchall()
    return [dict(r) for r in rows]


# ── OHLCV writes ──────────────────────────────────────────────────────────────

def upsert_ohlcv(stock_id: int, df: pd.DataFrame) -> int:
    """
    Insert rows from df into ohlcv. On conflict (same stock_id+date) do nothing.
    All rows for this stock are written in one transaction.
    Returns number of rows inserted.
    """
    rows = [
        (
            stock_id,
            str(idx.date()) if hasattr(idx, "date") else str(idx),
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            int(row["volume"]),
        )
        for idx, row in df.iterrows()
    ]
    if not rows:
        return 0

    with get_conn() as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO ohlcv (stock_id, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        inserted = conn.total_changes

    log.debug(f"db: upsert_ohlcv stock_id={stock_id} → {inserted} rows written")
    return inserted


def replace_ohlcv(stock_id: int, df: pd.DataFrame) -> int:
    """
    Delete and re-insert all OHLCV rows for stock_id.
    Used when a retroactive adjustment (split/bonus) is detected.
    Wrapped in a single transaction.
    """
    rows = [
        (
            stock_id,
            str(idx.date()) if hasattr(idx, "date") else str(idx),
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            int(row["volume"]),
        )
        for idx, row in df.iterrows()
    ]

    with get_conn() as conn:
        conn.execute("DELETE FROM ohlcv WHERE stock_id = ?", (stock_id,))
        conn.executemany(
            """
            INSERT INTO ohlcv (stock_id, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

    log.info(f"db: replace_ohlcv stock_id={stock_id} → {len(rows)} rows")
    return len(rows)


# ── OHLCV reads ───────────────────────────────────────────────────────────────

def get_ohlcv(symbol: str, days: int = 200) -> pd.DataFrame:
    """
    Return the most recent `days` rows for symbol as a DataFrame.
    Index is DatetimeIndex. Columns: open, high, low, close, volume.
    Returns empty DataFrame if symbol not found.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT o.date, o.open, o.high, o.low, o.close, o.volume
            FROM ohlcv o
            JOIN stocks s ON s.id = o.stock_id
            WHERE s.symbol = ?
            ORDER BY o.date DESC
            LIMIT ?
            """,
            (symbol, days),
        ).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df


def get_last_date(stock_id: int) -> str | None:
    """Return the most recent date string stored for this stock, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT date FROM ohlcv WHERE stock_id = ? ORDER BY date DESC LIMIT 1",
            (stock_id,),
        ).fetchone()
    return row["date"] if row else None


# ── Setup signals ─────────────────────────────────────────────────────────────

def upsert_signal(
    stock_id: int,
    setup_name: str,
    signal_date: str,
    signal: bool,
    metadata: dict | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO setup_signals (stock_id, setup_name, signal_date, signal, metadata)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(stock_id, setup_name, signal_date) DO UPDATE SET
                signal   = excluded.signal,
                metadata = excluded.metadata
            """,
            (stock_id, setup_name, signal_date, int(signal), json.dumps(metadata or {})),
        )
        conn.commit()


# ── Ingestion log ─────────────────────────────────────────────────────────────

def log_ingestion_run(
    status: str,
    total: int,
    successful: int,
    failed: int,
    failed_symbols: list[str],
    notes: str = "",
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ingestion_runs
                (run_at, status, total_stocks, successful, failed, failed_symbols, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.utcnow().isoformat(),
                status,
                total,
                successful,
                failed,
                json.dumps(failed_symbols),
                notes,
            ),
        )
        conn.commit()


# ── Adjustment log ────────────────────────────────────────────────────────────

def log_adjustment(stock_id: int, action_type: str, notes: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO adjustment_log (stock_id, action_type, notes) VALUES (?, ?, ?)",
            (stock_id, action_type, notes),
        )
        conn.commit()
