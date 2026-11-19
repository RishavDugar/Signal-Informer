"""
Verdict watch — every listed company named in tomorrow's verdict lists, whether
or not we were already tracking the case.

Tracking only cases we had discovered (web search / filings) leaves gaps: a
discovery pass can miss a case, and courts list some cases under a subsidiary's
name. Verdict lists are short (a few to ~30 judgments a day) and free, so the
cheapest complete answer is to read them in full every evening:

  Supreme Court   daily lists — items under 'JUDGMENT BY …' / 'FOR JUDGEMENT'
                  (numbered 1501+)
  Delhi HC        the 'j_' list is judgments-only

For each listed company found (F&O stocks + market cap ≥ ₹5,000 cr):
  • not tracked yet → a case is created (stage 'listed for pronouncement', next
    date = list date) and assessed, so it shows on the dashboard like any other.
  • one WhatsApp digest per list date, only if WhatsApp is up:
        ⚖️ Court verdicts due Fri 16 Oct (listed companies)
        🔴 Yes Bank — vs Axis Trustee Services (C.A. 3182/2025, Court 6) — could hurt (~-15%)
        ⚪ Coal India — vs Sujata Bora (C.A. 120/2026, Court 7) — looks routine
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from config import BASE_DIR
from litigation import db as ldb
from utils.logger import get_logger

log = get_logger("litigation.verdicts")

STATE = BASE_DIR / "db" / "cache" / "verdict_watch.json"


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"days": {}, "sent": []}


def _save(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    # keep ~3 weeks
    days = dict(sorted(st.get("days", {}).items())[-21:])
    STATE.write_text(json.dumps({"days": days, "sent": st.get("sent", [])[-1500:]},
                                indent=1, ensure_ascii=False), encoding="utf-8")


def find(text: str, court: str, list_name: str = "") -> list[dict]:
    """Listed companies in this schedule's verdict sections."""
    from litigation.matching import verdict_hits
    sc = "supreme" in court.lower()
    whole = (not sc) and list_name.lower().startswith("j_")      # Delhi HC judgments list
    if not (sc or whole):
        return []
    return verdict_hits(text, whole_doc_is_verdicts=whole, sc=sc)


def _ingest(h: dict, court: str, d: str, url: str) -> dict:
    """Create / update the case and assess it. Returns assessment fields."""
    import re
    from litigation.tracker import _ingest_case
    rows = [c for c in ldb.cases_for(h["symbol"], active_only=False) if c.get("status") != "duplicate"]
    # Same case already tracked? (a) the tracked-case scan of this very list just
    # dated it, or (b) its case number matches.
    nums = set(re.findall(r"(\d{2,6})(?:-\d{2,6})?\s*/\s*((?:19|20)\d{2})", h.get("case_no") or ""))
    same_forum = lambda c: ("supreme" in (c.get("court") or "").lower()) == ("supreme" in court.lower())  # noqa: E731
    existing = [c for c in rows if c.get("next_date") == d and same_forum(c)] or \
               [c for c in rows if nums and any(n in (c.get("case_number") or "") and y in (c.get("case_number") or "")
                                                for n, y in nums)]
    if existing:
        c = existing[0]
        if c.get("next_date") != d:
            _ingest_case(h["symbol"], {**c, "next_date": d, "stage": "listed_for_pronouncement",
                                       "listing_note": f"{h.get('court_no') or ''}, item {h.get('item') or '?'}",
                                       "date_source": f"{court} verdict list {d}"}, f"{court} verdict list")
        return ldb.query("SELECT * FROM lit_cases WHERE id=?", (c["id"],))[0]
    case = {"title": f"Verdict listed: {h.get('versus') or h['company']}"[:200],
            "court": court, "case_number": h.get("case_no"),
            "stage": "listed_for_pronouncement", "stage_date": date.today().isoformat(),
            "next_date": d, "listing_note": f"{h.get('court_no') or ''}, item {h.get('item') or '?'} — verdict list".strip(", "),
            "subject": h.get("versus"), "date_source": f"{court} verdict list {d}",
            "company_name": h["company"],
            "update": f"Listed for pronouncement of judgment on {d} ({url})"}
    try:
        _ingest_case(h["symbol"], case, f"{court} verdict list")
    except Exception as exc:
        log.warning(f"verdicts: ingest {h['symbol']} failed — {exc}")
    rows = ldb.query("SELECT * FROM lit_cases WHERE symbol=? AND next_date=? ORDER BY last_checked DESC LIMIT 1",
                     (h["symbol"], d))
    return rows[0] if rows else {}


def process(text: str, court: str, d: str, url: str, list_name: str = "") -> int:
    """Called by the auto-scan for every new schedule it downloads."""
    hits = find(text, court, list_name)
    if not hits:
        return 0
    st = _state()
    day = st["days"].setdefault(d, [])
    have = {(x["symbol"], x.get("case_no")) for x in day}
    new = 0
    for h in hits:
        if (h["symbol"], h.get("case_no")) in have:
            continue
        have.add((h["symbol"], h.get("case_no")))
        c = _ingest(h, court, d, url)
        day.append({"symbol": h["symbol"], "company": h["company"], "court": court,
                    "court_no": h.get("court_no"), "item": h.get("item"), "case_no": h.get("case_no"),
                    "versus": h.get("versus"), "petitioner": h.get("petitioner"),
                    "respondent": h.get("respondent"), "url": url, "case_id": c.get("id"),
                    "p_adverse": c.get("p_adverse"), "move_adverse": c.get("move_adverse"),
                    "move_favourable": c.get("move_favourable"), "status": c.get("status"),
                    "found": datetime.now().isoformat(timespec="minutes")})
        new += 1
    _save(st)
    log.info(f"verdicts: {court} {d} — {len(hits)} listed-company verdict item(s), {new} new")
    return new


def upcoming() -> list[dict]:
    today = date.today().isoformat()
    return [dict(x, date=d) for d, items in _state().get("days", {}).items() if d >= today for x in items]


def _line(x: dict) -> str:
    from litigation.insight import _name
    name = _name({"symbol": x["symbol"], "company_name": x["company"]})
    p, adv, fav = x.get("p_adverse"), x.get("move_adverse"), x.get("move_favourable")
    worst = max(abs(adv or 0), abs(fav or 0))
    if p is not None and worst >= 0.05:
        icon = "🔴"
        effect = (f"could hurt the stock (~{adv:+.0%})" if p >= 0.5 and adv else
                  f"could move the stock (up to {worst:.0%})")
    elif p is not None and worst >= 0.02:
        icon, effect = "🟠", f"modest impact likely (≤ {worst:.0%})"
    elif p is not None:
        icon, effect = "⚪", "looks routine"
    else:
        icon, effect = "⚪", "impact not estimated"
    from litigation.matching import name_regex
    rx = name_regex(x["company"])
    pet, res = x.get("petitioner") or "", x.get("respondent") or ""
    vs = res if (rx and rx.search(pet)) else pet
    vs = re.sub(r"\s+(AND\s+(ORS|ANR)|ETC)\.?$", "", vs, flags=re.I).strip()
    vs = " ".join(vs.split()[:7]).title().replace("M/S ", "").replace("M/S.", "")
    where = ", ".join(b for b in (x.get("case_no"), x.get("court_no")) if b)
    return f"{icon} *{name}* — {('vs ' + vs) if vs else ''} ({where}) — {effect}"


def send_digest() -> int:
    """One WhatsApp per verdict date with every listed company in it. Only when
    WhatsApp is up (otherwise nothing is marked sent and it retries next scan)."""
    from config import LITIGATION_DASHBOARD_URL, LITIGATION_ENABLED, LITIGATION_REMINDERS
    if not (LITIGATION_ENABLED and LITIGATION_REMINDERS):
        return 0
    st = _state()
    today = date.today().isoformat()
    sent = 0
    for d, items in sorted(st.get("days", {}).items()):
        key_items = [x for x in items if f"{d}|{x['symbol']}|{x.get('case_no')}" not in st["sent"]]
        if d < today or not key_items:
            continue
        rank = {"🔴": 0, "🟠": 1, "⚪": 2}
        by_sym: dict[str, list[dict]] = {}
        for x in key_items:                      # one line per company; batches of connected cases
            by_sym.setdefault(x["symbol"], []).append(x)
        lines = []
        for xs in by_sym.values():
            ln = _line(xs[0])
            if len(xs) > 1:
                ln += f" (+{len(xs) - 1} connected case{'s' if len(xs) > 2 else ''})"
            lines.append(ln)
        lines.sort(key=lambda s: rank.get(s[:1], 3))
        if len(lines) > 15:
            lines = lines[:15] + [f"…and {len(lines) - 15} more on the dashboard."]
        dd = date.fromisoformat(d)
        msg = "\n".join([f"*⚖️ Court verdicts due {dd:%a %d %b} — listed companies*", ""] + lines +
                        ["", "Verdicts usually come between 10:30 and 13:00.",
                         f"Details: {LITIGATION_DASHBOARD_URL}"])
        ok = ldb.wa_send([msg])
        if ok is None:                      # WhatsApp not up → do nothing now
            return sent
        if ok:
            st["sent"] += [f"{d}|{x['symbol']}|{x.get('case_no')}" for x in key_items]
            # the per-case 'new case / date change' alerts would repeat this — mark them done
            ids = [x["case_id"] for x in key_items if x.get("case_id")]
            if ids:
                ev = [e["id"] for e in ldb.unalerted_events() if e.get("case_id") in ids]
                if ev:
                    ldb.mark_alerted(ev)
            sent += 1
    _save(st)
    return sent
