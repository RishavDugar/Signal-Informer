"""
DB layer for news-based stock recommendations.

Uses the same SQLite DB as the rest of the application.
The table `news_recommendations` is defined in data/db._SCHEMA and
auto-created by data.db.init_db().
"""

from __future__ import annotations

from datetime import date, timedelta

from data.db import get_conn
from utils.logger import get_logger

log = get_logger("news_db")

_DEDUP_DAYS_DEFAULT = 28


def save_recommendations(recs: list[dict]) -> list[int]:
    """
    Persist a list of recommendation dicts to news_recommendations.
    Each dict must have: symbol, company_name, rec_date, catalyst, analysis.
    Optional: news_snippets, cmp, change_1d_pct, change_5d_pct, change_20d_pct.

    Returns list of inserted row ids (skips duplicates).
    """
    from config import NEWS_DEDUP_DAYS
    inserted: list[int] = []
    expires = (date.today() + timedelta(days=NEWS_DEDUP_DAYS)).isoformat()

    with get_conn() as conn:
        for r in recs:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO news_recommendations
                        (symbol, company_name, rec_date, catalyst,
                         news_snippets, analysis,
                         cmp, change_1d_pct, change_5d_pct, change_20d_pct,
                         expires_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol, rec_date) DO NOTHING
                    """,
                    (
                        r["symbol"],
                        r.get("company_name", r.get("company", "")),
                        r.get("rec_date", date.today().isoformat()),
                        r.get("catalyst", ""),
                        r.get("news_snippets", ""),
                        r.get("analysis", ""),
                        r.get("cmp"),
                        r.get("change_1d_pct"),
                        r.get("change_5d_pct"),
                        r.get("change_20d_pct"),
                        expires,
                    ),
                )
                if cursor.lastrowid and cursor.rowcount:
                    inserted.append(cursor.lastrowid)
            except Exception as exc:
                log.error(f"news_db: failed to save {r.get('symbol')} — {exc}")
        conn.commit()

    log.info(f"news_db: saved {len(inserted)}/{len(recs)} new recommendations")
    return inserted


def get_recently_recommended(days: int | None = None) -> set[str]:
    """
    Return the set of symbols recommended within the last `days` days.
    Used for deduplication: don't re-recommend a stock too soon.
    """
    from config import NEWS_DEDUP_DAYS
    d = days or NEWS_DEDUP_DAYS
    cutoff = (date.today() - timedelta(days=d)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM news_recommendations WHERE rec_date >= ?",
            (cutoff,),
        ).fetchall()
    symbols = {row["symbol"] for row in rows}
    log.info(f"news_db: {len(symbols)} symbol(s) recommended in last {d} days (dedup)")
    return symbols


def mark_whatsapp_sent(row_ids: list[int]) -> None:
    """Mark recommendations as sent via WhatsApp."""
    if not row_ids:
        return
    from datetime import datetime
    placeholders = ",".join("?" * len(row_ids))
    with get_conn() as conn:
        conn.execute(
            f"""
            UPDATE news_recommendations
               SET whatsapp_sent = 1, sent_at = ?
             WHERE id IN ({placeholders})
            """,
            [datetime.utcnow().isoformat()] + row_ids,
        )
        conn.commit()
    log.info(f"news_db: marked {len(row_ids)} recommendation(s) as sent")


def get_today_recommendations() -> list[dict]:
    """Return today's unsent recommendations (for retry logic)."""
    today = date.today().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, symbol, company_name, catalyst, analysis,
                   cmp, change_1d_pct, change_5d_pct, change_20d_pct
              FROM news_recommendations
             WHERE rec_date = ? AND whatsapp_sent = 0
            """,
            (today,),
        ).fetchall()
    return [dict(r) for r in rows]


def purge_expired() -> int:
    """Delete recommendations past their expiry date. Returns count deleted."""
    today = date.today().isoformat()
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM news_recommendations WHERE expires_at < ?", (today,)
        )
        conn.execute(
            "DELETE FROM scout_recommendations WHERE expires_at < ?", (today,)
        )
        deleted = conn.total_changes
        conn.commit()
    if deleted:
        log.info(f"news_db: purged {deleted} expired recommendation(s)")
    return deleted


# ── Scout picks (hidden gems / small-cap growth / smart money) ───────────────
#
# Mirrors the news_recommendations helpers above but scoped by `scout_type`,
# stored in a separate table, and deduplicated over a much shorter window
# (SCOUT_DEDUP_DAYS, default 5 — scout lenses re-scan a fast-moving feed daily,
# unlike the slower-moving mainstream-news cycle that NEWS_DEDUP_DAYS governs).

def save_scout_recommendations(scout_type: str, picks: list[dict]) -> list[int]:
    """
    Persist scout picks for one lens to scout_recommendations.
    Each dict must have: symbol, company (or company_name), catalyst, reasoning, analysis.
    Optional: cmp, change_1d_pct, change_5d_pct, change_20d_pct.

    Returns list of inserted row ids (skips same-day duplicates for this lens).
    """
    from config import SCOUT_DEDUP_DAYS
    inserted: list[int] = []
    today   = date.today().isoformat()
    expires = (date.today() + timedelta(days=SCOUT_DEDUP_DAYS)).isoformat()

    with get_conn() as conn:
        for p in picks:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO scout_recommendations
                        (scout_type, symbol, company_name, rec_date, catalyst,
                         reasoning, analysis,
                         cmp, change_1d_pct, change_5d_pct, change_20d_pct,
                         expires_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(scout_type, symbol, rec_date) DO NOTHING
                    """,
                    (
                        scout_type,
                        p["symbol"],
                        p.get("company_name", p.get("company", "")),
                        p.get("rec_date", today),
                        p.get("catalyst", ""),
                        p.get("reasoning", ""),
                        p.get("analysis", ""),
                        p.get("cmp"),
                        p.get("change_1d_pct"),
                        p.get("change_5d_pct"),
                        p.get("change_20d_pct"),
                        expires,
                    ),
                )
                if cursor.lastrowid and cursor.rowcount:
                    inserted.append(cursor.lastrowid)
            except Exception as exc:
                log.error(f"news_db: failed to save scout[{scout_type}] {p.get('symbol')} — {exc}")
        conn.commit()

    log.info(f"news_db: saved {len(inserted)}/{len(picks)} new scout[{scout_type}] recommendation(s)")
    return inserted


def get_recently_scouted(scout_type: str, days: int | None = None) -> set[str]:
    """
    Return the set of symbols surfaced by this scout lens within the last `days` days.
    Used for deduplication — don't resurface the same pick too soon.
    """
    from config import SCOUT_DEDUP_DAYS
    d = days or SCOUT_DEDUP_DAYS
    cutoff = (date.today() - timedelta(days=d)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT symbol FROM scout_recommendations
             WHERE scout_type = ? AND rec_date >= ?
            """,
            (scout_type, cutoff),
        ).fetchall()
    symbols = {row["symbol"] for row in rows}
    log.info(f"news_db: {len(symbols)} symbol(s) scouted[{scout_type}] in last {d} days (dedup)")
    return symbols


def clear_all() -> int:
    """
    Wipe ALL rows from news_recommendations AND scout_recommendations —
    resets every dedup window (28-day main news + 5-day scout) to empty.

    Does NOT touch ohlcv/stocks/setup_signals — only the news-analyzer's own
    tables. Use via `python -m news_analyzer.db --clear` (e.g. to force
    re-sending picks during testing, or to recover from bad/stale data).

    Returns the total number of rows deleted.
    """
    with get_conn() as conn:
        before_news  = conn.execute("SELECT COUNT(*) FROM news_recommendations").fetchone()[0]
        before_scout = conn.execute("SELECT COUNT(*) FROM scout_recommendations").fetchone()[0]
        conn.execute("DELETE FROM news_recommendations")
        conn.execute("DELETE FROM scout_recommendations")
        conn.commit()
    deleted = before_news + before_scout
    log.warning(
        f"news_db: CLEARED all news-analyzer history — "
        f"{before_news} news_recommendations + {before_scout} scout_recommendations "
        f"= {deleted} row(s) deleted. Dedup windows reset to empty."
    )
    return deleted


def mark_scout_whatsapp_sent(row_ids: list[int]) -> None:
    """Mark scout recommendations as sent via WhatsApp."""
    if not row_ids:
        return
    from datetime import datetime
    placeholders = ",".join("?" * len(row_ids))
    with get_conn() as conn:
        conn.execute(
            f"""
            UPDATE scout_recommendations
               SET whatsapp_sent = 1, sent_at = ?
             WHERE id IN ({placeholders})
            """,
            [datetime.utcnow().isoformat()] + row_ids,
        )
        conn.commit()
    log.info(f"news_db: marked {len(row_ids)} scout recommendation(s) as sent")


# ── CLI: maintenance commands ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    from data.db import init_db
    init_db()

    if "--clear" in sys.argv or "--reset" in sys.argv:
        n = clear_all()
        print(f"Cleared {n} row(s) — news_recommendations + scout_recommendations are now empty.")
        print("Dedup windows reset: the next pipeline run may re-send picks sent previously.")
    else:
        print("News-analyzer DB maintenance commands:")
        print()
        print("  python -m news_analyzer.db --clear")
        print("      Wipe ALL rows from news_recommendations + scout_recommendations.")
        print("      Resets the 28-day main-news dedup window AND the 5-day scout")
        print("      dedup window to empty (does not touch ohlcv/stocks/setup tables).")
