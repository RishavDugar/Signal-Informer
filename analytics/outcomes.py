"""
Outcome tracking — the feedback loop that measures whether the picks we sent
actually worked, net of costs, against what the backtest promised.

Flow:
  1. record_picks(...)      — called when a technical alert is sent: one row per
                              stock pick (entry = the NEXT session's open).
  2. update_outcomes()      — called each daily pipeline run: as new OHLCV lands,
                              fill entry/exit prices and the realised net return
                              once a pick reaches its horizon; mark it 'closed'.
  3. scorecard()/format_*   — aggregate realised vs expected over a trailing window.
  4. send_weekly_scorecard_if_due() — WhatsApp the scorecard on SCORECARD_WEEKDAY.

Entry/exit model mirrors the backtester: entry at D+1 open, exit at the close of
the bar `horizon_days` sessions later (horizon=1 => same-day close, used for the
intraday SELL book). Returns are netted by TRANSACTION_COST so realised numbers
are directly comparable to the after-cost backtest expectation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from config import TRANSACTION_COST, SCORECARD_DAYS, SCORECARD_WEEKDAY, DB_PATH
from data import db
from utils.logger import get_logger

log = get_logger("outcomes")

_MARKER = DB_PATH.parent / ".last_scorecard"


# ── Schema ─────────────────────────────────────────────────────────────────────

def _ensure() -> None:
    with db.get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pick_outcomes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT    NOT NULL,
                direction       TEXT    NOT NULL,
                signal_date     TEXT    NOT NULL,
                horizon_days    INTEGER NOT NULL,
                expected_return REAL,
                expected_conf   REAL,
                n_setups        INTEGER,
                setups          TEXT,
                entry_date      TEXT,
                entry_price     REAL,
                exit_date       TEXT,
                exit_price      REAL,
                realized_return REAL,
                status          TEXT    NOT NULL DEFAULT 'pending',
                created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
                updated_at      TEXT,
                UNIQUE(symbol, signal_date)
            )
            """
        )
        conn.commit()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ── Pure return math ───────────────────────────────────────────────────────────

def _realized_return(direction: str, entry_open: float, exit_close: float,
                     cost: float = TRANSACTION_COST) -> float | None:
    """Net realised return. SELL profits when price falls; both sides pay `cost`."""
    if not entry_open:
        return None
    if str(direction).upper() == "SELL":
        raw = (entry_open - exit_close) / entry_open
    else:
        raw = (exit_close - entry_open) / entry_open
    return raw - cost


# ── Recording ──────────────────────────────────────────────────────────────────

def record_picks(picks: list[dict], signal_date: str) -> int:
    """
    Insert one pending outcome row per sent pick. INSERT OR IGNORE keeps re-runs
    on the same signal_date idempotent. Returns the number of new rows.
    """
    if not picks or not signal_date:
        return 0
    _ensure()
    inserted = 0
    with db.get_conn() as conn:
        for p in picks:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO pick_outcomes
                    (symbol, direction, signal_date, horizon_days,
                     expected_return, expected_conf, n_setups, setups, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (p["symbol"], p["direction"], signal_date, int(p["horizon_days"]),
                 p.get("expected_return"), p.get("expected_conf"),
                 p.get("n_setups"), p.get("setups", "")),
            )
            inserted += cur.rowcount
        conn.commit()
    log.info(f"outcomes: recorded {inserted} new pick(s) for {signal_date} "
             f"({len(picks) - inserted} already tracked)")
    return inserted


# ── Filling realised returns ────────────────────────────────────────────────────

def update_outcomes(cost: float = TRANSACTION_COST) -> int:
    """
    For every pending pick, fill entry (D+1 open) and — once available — the exit
    (close `horizon_days` sessions later) and realised net return. Returns the
    number of picks newly closed this run.
    """
    _ensure()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pick_outcomes WHERE status = 'pending'"
        ).fetchall()

    closed = 0
    for r in rows:
        try:
            df = db.get_ohlcv(r["symbol"], days=180)
            if df.empty:
                continue
            dates  = [idx.strftime("%Y-%m-%d") for idx in df.index]
            opens  = [float(x) for x in df["open"].tolist()]
            closes = [float(x) for x in df["close"].tolist()]

            # Entry = first session strictly after the signal date.
            entry_i = next((i for i, d in enumerate(dates) if d > r["signal_date"]), None)
            if entry_i is None:
                continue  # no session yet after the signal — try again next run

            entry_date, entry_open = dates[entry_i], opens[entry_i]
            exit_i = entry_i + (int(r["horizon_days"]) - 1)

            if exit_i >= len(dates):
                # Entry known, horizon not yet reached — persist entry, stay pending.
                if r["entry_price"] is None:
                    with db.get_conn() as conn:
                        conn.execute(
                            "UPDATE pick_outcomes SET entry_date=?, entry_price=?, "
                            "updated_at=? WHERE id=?",
                            (entry_date, entry_open, _now(), r["id"]),
                        )
                        conn.commit()
                continue

            exit_date, exit_close = dates[exit_i], closes[exit_i]
            realized = _realized_return(r["direction"], entry_open, exit_close, cost)
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE pick_outcomes SET entry_date=?, entry_price=?, exit_date=?, "
                    "exit_price=?, realized_return=?, status='closed', updated_at=? WHERE id=?",
                    (entry_date, entry_open, exit_date, exit_close, realized, _now(), r["id"]),
                )
                conn.commit()
            closed += 1
        except Exception as exc:
            log.debug(f"outcomes: update failed for {r['symbol']} @ {r['signal_date']} — {exc}")
    return closed


# ── Scorecard ────────────────────────────────────────────────────────────────--

def scorecard(days: int = SCORECARD_DAYS) -> dict:
    """Aggregate realised vs expected performance over the trailing `days`."""
    _ensure()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with db.get_conn() as conn:
        closed = conn.execute(
            "SELECT * FROM pick_outcomes WHERE status='closed' AND signal_date >= ? "
            "AND realized_return IS NOT NULL",
            (cutoff,),
        ).fetchall()
        pending = conn.execute(
            "SELECT COUNT(*) FROM pick_outcomes WHERE status='pending' AND signal_date >= ?",
            (cutoff,),
        ).fetchone()[0]

    base = {"days": days, "n": 0, "pending": pending, "win_rate": None,
            "avg_realized": None, "avg_expected": None, "total_realized": None,
            "best": None, "worst": None, "by_direction": {}}
    if not closed:
        return base

    rr   = [c["realized_return"] for c in closed]
    exp  = [c["expected_return"] for c in closed if c["expected_return"] is not None]
    wins = sum(1 for x in rr if x > 0)
    n    = len(rr)

    def _dir(d: str) -> dict:
        sub = [c for c in closed if str(c["direction"]).upper() == d]
        if not sub:
            return {"n": 0, "win_rate": None, "avg_realized": None}
        srr = [c["realized_return"] for c in sub]
        return {"n": len(srr),
                "win_rate": sum(1 for x in srr if x > 0) / len(srr),
                "avg_realized": sum(srr) / len(srr)}

    best  = max(closed, key=lambda c: c["realized_return"])
    worst = min(closed, key=lambda c: c["realized_return"])

    return {
        "days"          : days,
        "n"             : n,
        "pending"       : pending,
        "win_rate"      : wins / n,
        "avg_realized"  : sum(rr) / n,
        "avg_expected"  : (sum(exp) / len(exp)) if exp else None,
        "total_realized": sum(rr),
        "best"          : {"symbol": best["symbol"].replace(".NS", "").replace(".BO", ""),
                           "ret": best["realized_return"]},
        "worst"         : {"symbol": worst["symbol"].replace(".NS", "").replace(".BO", ""),
                           "ret": worst["realized_return"]},
        "by_direction"  : {"BUY": _dir("BUY"), "SELL": _dir("SELL")},
    }


def format_scorecard(sc: dict) -> str:
    """Render a scorecard dict as an investor-readable WhatsApp message."""
    days = sc["days"]
    if not sc["n"]:
        return (f"*Pick Scorecard — last {days}d*\n"
                f"_No closed picks yet ({sc['pending']} still open). "
                f"Results will appear as picks reach their horizon._")

    def pct(v):
        return "—" if v is None else f"{v:+.2%}"
    def rate(v):
        return "—" if v is None else f"{v:.0%}"

    bd = sc["by_direction"]
    lines = [
        f"*Pick Scorecard — last {days}d*",
        f"_{sc['n']} closed · {sc['pending']} still open · net of costs_",
        "",
        f"✅ Win rate: {rate(sc['win_rate'])} ({int(round(sc['win_rate']*sc['n']))}/{sc['n']})",
        f"📊 Avg realised: {pct(sc['avg_realized'])}  (expected {pct(sc['avg_expected'])})",
        f"📈 Best: {pct(sc['best']['ret'])} {sc['best']['symbol']}",
        f"📉 Worst: {pct(sc['worst']['ret'])} {sc['worst']['symbol']}",
    ]
    parts = []
    for d in ("BUY", "SELL"):
        sub = bd.get(d, {})
        if sub.get("n"):
            parts.append(f"{d} {sub['n']} ({rate(sub['win_rate'])}, {pct(sub['avg_realized'])})")
    if parts:
        lines.append("  " + "  ·  ".join(parts))
    lines += ["", "_Realised return vs the backtest expectation. "
              "A persistent gap means the model is drifting._"]
    return "\n".join(lines)


# ── Weekly send ──────────────────────────────────────────────────────────────--

def send_scorecard_now(days: int = SCORECARD_DAYS) -> bool:
    """Build and send the scorecard immediately (manual / dashboard trigger)."""
    from notifications.whatsapp import send_whatsapp
    return send_whatsapp(format_scorecard(scorecard(days)))


def send_weekly_scorecard_if_due() -> bool:
    """
    Send the scorecard once on SCORECARD_WEEKDAY (0=Mon..6=Sun). A date marker
    file dedupes against multiple runs / retries on the same day.
    """
    if date.today().weekday() != SCORECARD_WEEKDAY:
        return False
    today = date.today().isoformat()
    try:
        if _MARKER.exists() and _MARKER.read_text(encoding="utf-8").strip() == today:
            return False
    except Exception:
        pass

    update_outcomes()  # make sure the week's matured picks are counted
    ok = send_scorecard_now()
    if ok:
        log.info("outcomes: weekly scorecard sent")
        try:
            _MARKER.write_text(today, encoding="utf-8")
        except Exception:
            pass
    else:
        log.error("outcomes: weekly scorecard send failed")
    return ok
