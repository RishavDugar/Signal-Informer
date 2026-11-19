"""
Storage for the litigation tracker (same SQLite DB as everything else).

  lit_cases   one row per (symbol, case) — the live case card + latest assessment
  lit_events  append-only history: new case, stage changes, rulings, filings
  lit_filings BSE announcements already seen (dedup + legal classification)
  lit_scan    per-symbol bookkeeping (last web discovery)
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from contextlib import contextmanager

from data.db import get_conn
from utils.logger import get_logger

log = get_logger("litigation.db")

# Ordered: later stages are closer to a price-moving ruling.
STAGES = ["filed", "pending", "hearing", "arguments_concluded", "reserved",
          "listed_for_pronouncement", "decided", "disposed", "unknown"]
# Stages at which a ruling can land any day → binary-event risk.
HOT_STAGES = {"arguments_concluded", "reserved", "listed_for_pronouncement"}
CLOSED_STAGES = {"decided", "disposed"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lit_cases (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT NOT NULL,
    case_key          TEXT NOT NULL,
    title             TEXT,
    court             TEXT,
    case_number       TEXT,
    counterparty      TEXT,
    company_role      TEXT,
    subject           TEXT,
    claim_cr          REAL,
    stage             TEXT,
    stage_date        TEXT,
    next_date         TEXT,
    outcome           TEXT,
    mcap_cr           REAL,
    materiality       REAL,
    p_adverse         REAL,
    move_adverse      REAL,
    move_favourable   REAL,
    expected_abs_move REAL,
    confidence        TEXT,
    rationale         TEXT,
    trade_idea        TEXT,
    implied_move      REAL,
    sources           TEXT,
    status            TEXT DEFAULT 'active',
    first_seen        TEXT,
    last_checked      TEXT,
    last_assessed     TEXT,
    UNIQUE(symbol, case_key)
);
CREATE INDEX IF NOT EXISTS idx_lit_cases_symbol ON lit_cases(symbol);
CREATE TABLE IF NOT EXISTS lit_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id   INTEGER,
    symbol    TEXT,
    ts        TEXT,
    kind      TEXT,
    old_value TEXT,
    new_value TEXT,
    detail    TEXT,
    alerted   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lit_filings (
    news_id   TEXT PRIMARY KEY,
    symbol    TEXT,
    scrip     TEXT,
    dt        TEXT,
    subject   TEXT,
    attachment TEXT,
    is_legal  INTEGER,
    text      TEXT,
    processed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lit_attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     INTEGER,
    uploaded    TEXT,
    filename    TEXT,
    path        TEXT,
    source      TEXT,
    doc_type    TEXT,
    doc_date    TEXT,
    case_found  INTEGER,
    summary     TEXT,
    analysis    TEXT
);
CREATE TABLE IF NOT EXISTS lit_checks (
    key         TEXT PRIMARY KEY,
    case_id     INTEGER,
    done_at     TEXT,
    reminded_at TEXT,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS lit_scan (
    symbol          TEXT PRIMARY KEY,
    last_discovered TEXT,
    n_cases         INTEGER
);
"""

_INIT = False


@contextmanager
def _tx():
    """data.db.get_conn() never commits — wrap it so every block here does."""
    with get_conn() as conn:
        yield conn
        conn.commit()


def init() -> None:
    global _INIT
    if _INIT:
        return
    with _tx() as conn:
        conn.executescript(_SCHEMA)
        have = {r[1] for r in conn.execute("PRAGMA table_info(lit_cases)").fetchall()}
        for col in ("listing_note", "date_source", "company_name", "search_json"):
            if col not in have:
                conn.execute(f"ALTER TABLE lit_cases ADD COLUMN {col} TEXT")
    _INIT = True


def case_key(c: dict) -> str:
    """Stable identity for a case: the case number if we have one, else a
    normalised court+title slug (so re-discovery updates instead of duplicating)."""
    num = re.sub(r"[^a-z0-9]", "", (c.get("case_number") or "").lower())
    if num and num not in ("unknown", "na", "none"):
        return f"no:{num}"[:120]
    slug = re.sub(r"[^a-z0-9]+", "-", f"{c.get('court','')}-{c.get('title') or c.get('subject','')}".lower())
    return f"t:{slug.strip('-')}"[:120]


_STOP = {"the", "and", "of", "for", "on", "in", "vs", "v", "against", "by", "to", "a",
         "an", "case", "appeal", "petition", "order", "dispute", "with", "over"}


def _tokens(c: dict) -> set[str]:
    t = re.findall(r"[a-z0-9]+", (c.get("title") or c.get("subject") or "").lower())
    return {w.replace("off", "down") for w in t if w not in _STOP and len(w) > 1}


def _similar(conn, symbol: str, c: dict) -> dict | None:
    """Existing non-duplicate row for the same symbol whose title/subject overlaps
    ≥ 50% (Jaccard) — LLM passes re-word titles run to run ('write-down' vs
    'write-off'), and a slug key alone would duplicate the case."""
    mine = _tokens(c)
    if not mine:
        return None
    best, best_j = None, 0.0
    for row in conn.execute("SELECT * FROM lit_cases WHERE symbol=? AND status!='duplicate'",
                            (symbol,)).fetchall():
        other = _tokens(row)
        j = len(mine & other) / max(1, len(mine | other))
        if j > best_j:
            best, best_j = row, j
    return best if best_j >= 0.5 else None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def upsert_case(symbol: str, c: dict) -> tuple[int, dict | None]:
    """Insert or update a case. Returns (case_id, previous_row_or_None)."""
    init()
    key = c.get("case_key") or case_key(c)
    cols = ["title", "court", "case_number", "counterparty", "company_role", "subject",
            "claim_cr", "stage", "stage_date", "next_date", "outcome", "sources",
            "listing_note", "date_source", "company_name"]
    vals = {k: c.get(k) for k in cols}
    if isinstance(vals.get("sources"), (list, dict)):
        vals["sources"] = json.dumps(vals["sources"])[:4000]
    with _tx() as conn:
        conn.row_factory = _dict_factory
        prev = conn.execute("SELECT * FROM lit_cases WHERE symbol=? AND case_key=?",
                            (symbol, key)).fetchone()
        if not prev and not c.get("case_key"):
            prev = _similar(conn, symbol, c)      # same case, re-worded title
        if prev:
            # Never overwrite a known value with None/unknown from a vaguer pass.
            upd = {k: v for k, v in vals.items() if v not in (None, "", "unknown")}
            upd["last_checked"] = _now()
            if upd.get("stage") in CLOSED_STAGES and prev.get("status") == "active":
                upd["status"] = "closed"
            sets = ", ".join(f"{k}=?" for k in upd)
            conn.execute(f"UPDATE lit_cases SET {sets} WHERE id=?", (*upd.values(), prev["id"]))
            return prev["id"], prev
        vals.update(symbol=symbol, case_key=key, first_seen=_now(), last_checked=_now(),
                    status="closed" if vals.get("stage") in CLOSED_STAGES else "active")
        cur = conn.execute(f"INSERT INTO lit_cases ({', '.join(vals)}) VALUES "
                           f"({', '.join('?' * len(vals))})", tuple(vals.values()))
        return cur.lastrowid, None


def save_assessment(case_id: int, a: dict) -> None:
    init()
    cols = ["mcap_cr", "materiality", "p_adverse", "move_adverse", "move_favourable",
            "expected_abs_move", "confidence", "rationale", "trade_idea", "implied_move"]
    upd = {k: a.get(k) for k in cols if k in a}
    upd["last_assessed"] = _now()
    with _tx() as conn:
        conn.execute(f"UPDATE lit_cases SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?",
                     (*upd.values(), case_id))


def add_event(case_id: int | None, symbol: str, kind: str, old=None, new=None,
              detail: str = "") -> int:
    init()
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO lit_events (case_id, symbol, ts, kind, old_value, new_value, detail) "
            "VALUES (?,?,?,?,?,?,?)", (case_id, symbol, _now(), kind,
                                       None if old is None else str(old),
                                       None if new is None else str(new), detail[:2000]))
        return cur.lastrowid


def _dict_factory(cur, row):
    return {d[0]: row[i] for i, d in enumerate(cur.description)}


def query(sql: str, args: tuple = ()) -> list[dict]:
    init()
    with _tx() as conn:
        conn.row_factory = _dict_factory
        return conn.execute(sql, args).fetchall()


def execute(sql: str, args: tuple = ()) -> None:
    init()
    with _tx() as conn:
        conn.execute(sql, args)


def cases_for(symbol: str, active_only: bool = True) -> list[dict]:
    q = "SELECT * FROM lit_cases WHERE symbol=?" + (" AND status='active'" if active_only else "")
    return query(q + " ORDER BY COALESCE(expected_abs_move,0) DESC", (symbol,))


def active_cases() -> list[dict]:
    return query("SELECT * FROM lit_cases WHERE status='active'")


def unalerted_events() -> list[dict]:
    return query("SELECT e.*, c.title, c.court, c.stage, c.p_adverse, c.move_adverse, "
                 "c.move_favourable, c.expected_abs_move, c.trade_idea, c.claim_cr, "
                 "c.materiality, c.implied_move, c.next_date, c.outcome "
                 "FROM lit_events e LEFT JOIN lit_cases c ON c.id=e.case_id "
                 "WHERE e.alerted=0 ORDER BY e.id")


def mark_alerted(ids: list[int]) -> None:
    if not ids:
        return
    init()
    with _tx() as conn:
        conn.executemany("UPDATE lit_events SET alerted=1 WHERE id=?", [(i,) for i in ids])


def seen_filing(news_id: str) -> bool:
    return bool(query("SELECT 1 FROM lit_filings WHERE news_id=?", (news_id,)))


def save_filing(f: dict) -> None:
    init()
    with _tx() as conn:
        conn.execute("INSERT OR IGNORE INTO lit_filings (news_id, symbol, scrip, dt, subject, "
                     "attachment, is_legal, text, processed) VALUES (?,?,?,?,?,?,?,?,?)",
                     (f["news_id"], f.get("symbol"), f.get("scrip"), f.get("dt"),
                      f.get("subject"), f.get("attachment"), int(bool(f.get("is_legal"))),
                      (f.get("text") or "")[:20000], int(bool(f.get("processed")))))


def mark_discovered(symbol: str, n: int) -> None:
    init()
    with _tx() as conn:
        conn.execute("INSERT INTO lit_scan(symbol,last_discovered,n_cases) VALUES(?,?,?) "
                     "ON CONFLICT(symbol) DO UPDATE SET last_discovered=excluded.last_discovered, "
                     "n_cases=excluded.n_cases", (symbol, _now(), n))


def last_discovered() -> dict[str, str]:
    return {r["symbol"]: r["last_discovered"] for r in query("SELECT * FROM lit_scan")}


# ── Attachments (user-downloaded documents) ───────────────────────────────────

def add_attachment(case_id: int | None, filename: str, path: str, source: str,
                   result: dict) -> int:
    init()
    with _tx() as conn:
        cur = conn.execute(
            "INSERT INTO lit_attachments (case_id, uploaded, filename, path, source, doc_type, "
            "doc_date, case_found, summary, analysis) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (case_id, _now(), filename, path, source, result.get("doc_type"), result.get("doc_date"),
             int(bool(result.get("case_found"))), (result.get("summary") or "")[:2000],
             json.dumps(result, ensure_ascii=False)[:20000]))
        return cur.lastrowid


def attachments(case_id: int | None = None, limit: int = 200) -> list[dict]:
    if case_id is None:
        return query("SELECT * FROM lit_attachments ORDER BY id DESC LIMIT ?", (limit,))
    return query("SELECT * FROM lit_attachments WHERE case_id=? ORDER BY id DESC", (case_id,))


# ── Check queue state (what the user has done / been reminded of) ─────────────

def check_state() -> dict[str, dict]:
    return {r["key"]: r for r in query("SELECT * FROM lit_checks")}


def mark_check(key: str, case_id: int | None, done: bool = True, note: str = "") -> None:
    init()
    with _tx() as conn:
        conn.execute("INSERT INTO lit_checks(key, case_id, done_at, note) VALUES(?,?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET done_at=excluded.done_at, note=excluded.note",
                     (key, case_id, _now() if done else None, note))


def mark_reminded(keys: list[str]) -> None:
    if not keys:
        return
    init()
    with _tx() as conn:
        for k in keys:
            conn.execute("INSERT INTO lit_checks(key, reminded_at) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET reminded_at=excluded.reminded_at", (k, _now()))


# ── WhatsApp send guard (shared by reminders + alerts) ────────────────────────

def wa_send(messages: list[str]) -> bool | None:
    """Send legal messages to LITIGATION_GROUP only if the WhatsApp bridge is
    ALREADY up. Returns True (sent), False (send attempted and failed) or None
    (bridge not ready → nothing sent, nothing attempted; caller leaves the items
    pending so they go out on a later cycle). Never auto-starts the bridge."""
    from config import LITIGATION_GROUP, WHATSAPP_BACKEND
    try:
        from notifications.whatsapp import bridge_health
        h = bridge_health()
    except Exception as exc:
        log.info(f"wa_send: bridge probe failed ({exc}) — not sending")
        return None
    if WHATSAPP_BACKEND != "bridge" or not h.get("ready"):
        log.info(f"wa_send: WhatsApp not ready (state={h.get('state')}) — holding "
                 f"{len(messages)} legal message(s) for a later cycle")
        return None
    from notifications.whatsapp import send_to_group
    ok = True
    for m in messages:
        ok = send_to_group(LITIGATION_GROUP, m) and ok
    return ok
