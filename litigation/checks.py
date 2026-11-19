"""
Check queue — tells you WHAT to look up yourself, WHERE, and WHEN.

The captcha-gated court pages can't be scraped, so the system works out from each
case's stage and dates when a human look is worth it, links the exact official
page, and reminds you on WhatsApp at that time. You download / screenshot the
page and attach it on the dashboard; Claude reads it and updates the case.

Rules (cases in scope: material + near-threshold tiers, plus any lower-tier case
that is reserved or has a date inside the next week):
  next date D known
     • evening before D  — check D's cause list: listed? court / item no.?
     • morning of D      — it's listed today: position before the open
     • evening of D      — download today's order (or judgment) and attach it
  judgment reserved / arguments concluded / listed for pronouncement (no date)
     • every evening (weekly for lower tiers) — check tomorrow's cause list for
       the pronouncement ('FOR JUDGMENT' section)
  pending with no known date
     • weekly (Monday morning) — case status for the next listing date
  decided in the last 7 days with no judgment attached
     • now — download the judgment / order PDF and attach it

Times come from LITIGATION_MORNING / LITIGATION_EVENING (IST, "HH:MM").
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from litigation import db as ldb
from litigation import portals
from litigation.analyzer import tier
from litigation.db import CLOSED_STAGES, HOT_STAGES
from utils.logger import get_logger

log = get_logger("litigation.checks")

_TIER_RANK = {"material": 2, "near": 1, "below": 0}
# Regulators act by orders, not listed hearings.
_REGULATOR = re.compile(r"\bsebi\b|securities and exchange board|competition commission|\bcci\b|"
                        r"reserve bank|\brbi\b|enforcement directorate|income tax department|\bcit\s*\(a|"
                        r"\bgst\b|\birdai\b|\bdot\b|department of telecom|ministry", re.I)


def _hhmm(s: str, default: time) -> time:
    try:
        h, m = s.split(":")
        return time(int(h), int(m))
    except Exception:
        return default


def _slots() -> tuple[time, time]:
    from config import LITIGATION_EVENING, LITIGATION_MORNING
    return _hhmm(LITIGATION_MORNING, time(9, 0)), _hhmm(LITIGATION_EVENING, time(19, 30))


def auto_court(c: dict) -> bool:
    """Courts whose daily/advance schedules are public PDFs we scan every evening
    (litigation.portals.auto_cause_lists): Supreme Court, Delhi High Court, NCLAT."""
    ct = (c.get("court") or "").lower()
    return "supreme" in ct or "nclat" in ct or ("delhi" in ct and "high court" in ct)


def _prev_workday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _next_workday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _pick_links(links: list[dict], *words: str) -> list[dict]:
    """Official links whose label matches any word, then the rest; news last."""
    off = [l for l in links if l["kind"] == "official"]
    hit = [l for l in off if any(w in l["label"].lower() for w in words)]
    rest = [l for l in off if l not in hit]
    news = [l for l in links if l["kind"] == "news"][:1]
    return (hit + rest)[:3] + news


def _case_ref(c: dict) -> str:
    bits = [c.get("case_number") or "", c.get("listing_note") or ""]
    return " · ".join(b for b in bits if b and b.lower() != "unknown")


def build(now: datetime | None = None, horizon_days: int = 7) -> list[dict]:
    """All checks due up to `horizon_days` ahead (and anything overdue, not done)."""
    now = now or datetime.now()
    today = now.date()
    morning, evening = _slots()
    soon = today + timedelta(days=horizon_days)
    state = ldb.check_state()
    rows = ldb.query("SELECT * FROM lit_cases WHERE status IN ('active','closed','immaterial')")
    atts = {}
    for a in ldb.query("SELECT case_id, MAX(uploaded) last, GROUP_CONCAT(doc_type) types "
                       "FROM lit_attachments GROUP BY case_id"):
        atts[a["case_id"]] = a
    out: list[dict] = []

    def add(c, key, due_d, due_t, action, links, why):
        st = state.get(key, {})
        if st.get("done_at"):
            return
        # Schedules we read ourselves every evening (free PDFs, no captcha) → no manual check
        if key.split(":")[1] in ("prelist", "pronounce") and auto_court(c):
            return
        due = datetime.combine(due_d, due_t)
        if due.date() > soon:
            return
        status = "overdue" if due < now - timedelta(hours=12) else \
                 "due" if due <= now else "today" if due.date() == today else "upcoming"
        t, reason = tier(c)
        out.append({"key": key, "kind": key.split(":")[1], "case_id": c["id"], "symbol": c["symbol"], "tier": t,
                    "title": c.get("title"), "court": c.get("court"), "stage": c.get("stage"),
                    "case_ref": _case_ref(c), "due": due.isoformat(timespec="minutes"),
                    "status": status, "action": action, "why": why, "links": links,
                    "auto": auto_court(c),
                    "reminded": bool(st.get("reminded_at"))})

    for c in rows:
        t, _ = tier(c)
        stage = c.get("stage") or "unknown"
        nd = c.get("next_date")
        nd_d = date.fromisoformat(nd) if nd and nd[:4].isdigit() else None
        dated_soon = nd_d is not None and today <= nd_d <= soon
        if not (t in ("material", "near") or stage in HOT_STAGES or dated_soon):
            continue
        if c.get("status") == "immaterial" and not (stage in HOT_STAGES or dated_soon or t == "near"):
            continue
        links = portals.official_links(c)
        court = c.get("court") or "the court"

        if stage in CLOSED_STAGES:
            sd = c.get("stage_date") or c.get("next_date") or ""
            recent = sd >= (today - timedelta(days=7)).isoformat()
            has_judgment = "judgment" in (atts.get(c["id"], {}).get("types") or "")
            if recent and not has_judgment:
                add(c, f"{c['id']}:judgment", today, now.time() if now.time() < evening else evening,
                    "The verdict is out. Download the verdict PDF and attach it — Claude will read "
                    "what the court decided and how much it could move the stock.",
                    _pick_links(links, "verdict", "notes"), "verdict given in the last 7 days")
            continue

        if nd_d and nd_d >= today:
            pre = _prev_workday(nd_d)
            if pre >= today:          # a pre-check whose evening has passed is moot
                add(c, f"{c['id']}:prelist:{nd}", pre, evening,
                    f"Open the {court} schedule for {nd_d:%a %d %b} and confirm the case is on it"
                    + (" — if it's in the 'FOR JUDGMENT' section, the verdict comes that morning."
                       if stage in HOT_STAGES else "."),
                    _pick_links(links, "schedule"), f"hearing on {nd}")
            add(c, f"{c['id']}:dayof:{nd}", nd_d, morning,
                f"The case is heard today in {court}. If you plan to trade it, decide before the market "
                f"opens; keep the news link open during the day.", _pick_links(links, "schedule", "case page"),
                f"hearing on {nd}")
            add(c, f"{c['id']}:outcome:{nd}", nd_d, evening,
                "Download today's hearing notes (or the verdict, if given) and attach them — Claude "
                "will read what happened and the next date.",
                _pick_links(links, "notes", "verdict", "case page"), f"heard on {nd}")
        elif stage in HOT_STAGES:
            since = ""
            if c.get("stage_date"):
                k = (today - date.fromisoformat(c["stage_date"][:10])).days
                since = f" ({k} days ago)" if k > 0 else ""
            if t in ("material", "near"):
                key, due_d = f"{c['id']}:pronounce:{today}", today
            else:                                   # lower tier → weekly
                monday = today - timedelta(days=today.weekday())
                key, due_d = f"{c['id']}:pronounce:w{monday}", today
            add(c, key, due_d, evening,
                f"The hearing is over and the verdict could come any day{since}. Open tomorrow's "
                f"{court} schedule ({_next_workday(today):%a %d %b}) — if the case is in the 'FOR JUDGMENT' "
                f"section, the verdict comes tomorrow morning.",
                _pick_links(links, "schedule"), "verdict pending — could come any day")
        else:
            monday = today - timedelta(days=today.weekday())
            if _REGULATOR.search(court) and not re.search(r"tribunal|appellate|\bsat\b", court, re.I):
                action = (f"{court} decides by orders, not hearing dates. Open its orders page, search the "
                          f"company name (below) and look for a new order in this matter — e.g. the final "
                          f"order. Attach it if there is one; otherwise just tick Done.")
                why = "watching for a new regulator order"
            elif auto_court(c):
                action = ("Next hearing date unknown. The nightly schedule scan will flag it the evening "
                          f"before it's listed — to know sooner, search the {court} case page with the details "
                          "below and look for 'next date' / 'tentatively listed on'; attach the page.")
                why = "next hearing date unknown"
            else:
                action = (f"We don't know the next hearing date. Search the {court} case page with the "
                          "details below, look for 'next date' / 'tentatively listed on', and attach the page.")
                why = "next hearing date unknown"
            # one per week; shown as due today until done (never "overdue")
            add(c, f"{c['id']}:status:w{monday}", today, morning if now.time() >= morning else now.time(),
                action, _pick_links(links, "case page", "find case", "orders"), why)

    out.sort(key=lambda x: ({"overdue": 0, "due": 1, "today": 2, "upcoming": 3}[x["status"]],
                            x["due"], -_TIER_RANK.get(x["tier"], 0)))
    return out


def complete_for_case(case_id: int, upto: date | None = None) -> int:
    """Mark a case's checks due up to `upto` (default tomorrow) as done — called
    when the user attaches a document for it."""
    upto = upto or (date.today() + timedelta(days=1))
    n = 0
    for chk in build():
        if chk["case_id"] == case_id and chk["due"][:10] <= upto.isoformat():
            ldb.mark_check(chk["key"], case_id, True, "auto: document attached")
            n += 1
    return n


# ── WhatsApp reminders ────────────────────────────────────────────────────────

def _fmt(chks: list[dict]) -> str:
    """Short heads-up: one plain sentence per case (deduped), one link for details."""
    from config import LITIGATION_DASHBOARD_URL
    from litigation.insight import one_liner
    icon = {"material": "🔴", "near": "🟠", "below": "⚪"}
    rows = {r["id"]: r for r in ldb.query("SELECT * FROM lit_cases")}
    seen, lines = set(), [f"*⚖️ Legal heads-up — {datetime.now():%a %d %b}*", ""]
    routine = 0
    for c in chks:
        if c["case_id"] in seen:
            continue
        seen.add(c["case_id"])
        if c["kind"] == "status":
            routine += 1
            continue
        case = rows.get(c["case_id"])
        if case:
            lines.append(f"{icon.get(c['tier'], '⚪')} {one_liner(case, c['tier'])}")
    if routine:
        lines.append(f"🗓️ Weekly: {routine} case(s) need their next hearing date looked up.")
    if len(lines) == 2:
        return ""
    lines += ["", f"Details & links: {LITIGATION_DASHBOARD_URL}"]
    return "\n".join(lines)


_LAST_RUN: datetime | None = None


def send_due_reminders(force: bool = False) -> int:
    """Send one WhatsApp digest of checks that are due now and not yet reminded.
    Cheap enough to call every listener poll; self-throttles to once a minute."""
    global _LAST_RUN
    now = datetime.now()
    if not force and _LAST_RUN and (now - _LAST_RUN).total_seconds() < 60:
        return 0
    _LAST_RUN = now
    from config import LITIGATION_ENABLED, LITIGATION_GROUP, LITIGATION_REMINDERS
    if not (LITIGATION_ENABLED and LITIGATION_REMINDERS):
        return 0
    # build() already limits scope to material/near cases + hot or dated ones.
    due = [c for c in build(now) if c["status"] in ("due", "overdue") and not c["reminded"]
           # routine "find the next date" checks: WhatsApp only for material cases, Mondays
           and (c["kind"] != "status" or (c["tier"] == "material" and now.weekday() == 0))]
    if not due:
        return 0
    if not LITIGATION_GROUP:
        log.info("reminders: LITIGATION_GROUP not set — not sending")
        return 0
    try:
        msg = _fmt(due[:12])
        ok = ldb.wa_send([msg]) if msg else True
    except Exception as exc:
        log.warning(f"reminders: send failed — {exc}")
        ok = False
    if ok is None:                 # WhatsApp not up → do nothing, retry next cycle
        return 0
    if ok:
        ldb.mark_reminded([c["key"] for c in due[:12]])
    log.info(f"reminders: {len(due)} due → {'sent' if ok else 'SEND FAILED'}")
    return len(due) if ok else 0
