"""
Plain-English layer for a busy reader: for every case, in one glance —
  verdict    Act now / Prepare / Watch / Low priority
  headline   "If the court rules against X (68% likely) the stock could fall ~5%…"
  priced     how that compares with what options already price
  when       when the ruling could land
  why_time   why it's worth (or not worth) your time, and how long a check takes
  score      attention score used to rank everything on the page

Deterministic: built only from the stored assessment, stage and dates (no LLM),
so the page and WhatsApp always say the same thing.
"""

from __future__ import annotations

from datetime import date

from litigation.db import CLOSED_STAGES, HOT_STAGES

_STAGE_TXT = {"reserved": "hearing over", "arguments_concluded": "arguments over",
              "listed_for_pronouncement": "verdict date fixed"}


def _days(d: str | None) -> int | None:
    try:
        return (date.fromisoformat(d[:10]) - date.today()).days if d else None
    except ValueError:
        return None


def _fmt_day(d: str) -> str:
    n = _days(d)
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    return date.fromisoformat(d[:10]).strftime("%a %d %b")


def _name(c: dict) -> str:
    import re
    n = (c.get("company_name") or c.get("symbol") or "").strip()
    n = re.sub(r"\s+", " ", n)
    n = re.sub(r"\s+(LIMITED|Limited|LTD\.?|Ltd\.?)$", "", n).strip(" .,")
    if n.isupper():                               # 'VODAFONE IDEA' → 'Vodafone Idea'
        n = " ".join(w if len(w) <= 4 else w.capitalize() for w in n.split())   # keep IFCI, BGR
    return n or c.get("symbol", "")


def _when(c: dict) -> tuple[str, float, str, str]:
    """(label, urgency 0-1, what-to-check hint, kind)."""
    stage, nd = c.get("stage"), c.get("next_date")
    n = _days(nd)
    hot = stage in HOT_STAGES
    if stage in CLOSED_STAGES:
        sd = c.get("stage_date") or nd
        k = _days(sd)
        recent = k is not None and k >= -5
        return (f"Verdict given {_fmt_day(sd)}" if sd else "Verdict given", 0.55 if recent else 0.05,
                "read the verdict", "decided")
    if n is not None and 0 <= n <= 1:
        if hot:
            return (f"Verdict {_fmt_day(nd)}", 1.0, "check the court schedule", "verdict_soon")
        return (f"Hearing {_fmt_day(nd)}", 0.45, "check the court schedule", "hearing_soon")
    if n is not None and 0 <= n <= 7:
        if hot:
            return (f"Verdict {_fmt_day(nd)}", 0.8, "check the court schedule the evening before", "verdict_soon")
        return (f"Hearing {_fmt_day(nd)}", 0.35, "check the court schedule the evening before", "hearing_soon")
    if hot:
        k = _days(c.get("stage_date"))
        ago = f", {-k}d ago" if k is not None and k < 0 else ""
        return (f"Any day ({_STAGE_TXT[stage]}{ago})", 0.7, "check tomorrow's court schedule",
                "any_day")
    if n is not None and n > 7:
        return (f"Next hearing {_fmt_day(nd)}", 0.2, "nothing until the week before", "later")
    return ("Date unknown", 0.15, "look up the next hearing date once a week", "unknown")


def one_liner(c: dict, tier: str = "below") -> str:
    """The single WhatsApp sentence for a case — no jargon, no numbers soup."""
    i = insight(c, tier)
    name, kind = f"*{i['name']}*", i["when_kind"]
    p, adv = c.get("p_adverse"), c.get("move_adverse")
    hurt = p is not None and p >= 0.5
    effect = (f"could hurt {name}" + (f" (about {adv:+.0%})" if adv else "")) if hurt else \
             (f"could move {name}" + (f" (up to {i['worst']:.0%} either way)" if i.get("worst") else ""))
    court = c.get("court") or "the court"
    day = i["when"].split(" ", 1)[-1]
    if kind == "verdict_soon":
        return f"There seems to be a court verdict that {effect} — expected {day} ({court})."
    if kind == "any_day":
        return (f"A court verdict that {effect} could come any day. Tomorrow's {court} schedule "
                f"comes out this evening — worth a 2-minute look.")
    if kind == "hearing_soon":
        return f"{name} has a court hearing {day} ({court}) — usually no verdict yet, but it sets the next date."
    if kind == "decided":
        o = (c.get("outcome") or "").lower()
        side = "against" if o.startswith("adverse") else "for" if o.startswith("favourable") else "on"
        return f"The court has ruled {side} {name} — the stock may keep moving for a few days."
    return f"No hearing date known yet for a case that {effect} — weekly check."


def insight(c: dict, tier: str = "below") -> dict:
    p, adv, fav = c.get("p_adverse"), c.get("move_adverse"), c.get("move_favourable")
    im = c.get("implied_move")
    when, urgency, hint, kind = _when(c)
    name = _name(c)
    out = {"when": when, "urgency": urgency, "when_kind": kind, "name": name}

    if p is None or adv is None or fav is None:
        out.update(headline=f"{name}: {c.get('title') or 'legal case'} — impact not estimated yet.",
                   priced="", score=0.0, priority="low", verdict="Low priority",
                   why_time="Not estimated yet — no action needed.", worst=None, edge=None, typical=None)
        return out

    worst = max(abs(adv), abs(fav))
    typical = p * abs(adv) + (1 - p) * abs(fav)      # probability-weighted size of the move
    edge = worst / im if im else None
    decided = c.get("stage") in CLOSED_STAGES

    if decided:
        o = (c.get("outcome") or "").lower()
        side = "against" if o.startswith("adverse") else "for" if o.startswith("favourable") else "mixed"
        headline = (f"The court ruled {side} {name}" if side != "mixed" else f"Mixed verdict for {name}") +                    (f" — expected stock reaction {adv:+.0%}." if side == "against" else
                    f" — expected stock reaction {fav:+.0%}." if side == "for" else ".")
    else:
        headline = (f"If the court rules against {name} ({p:.0%} likely), the stock could move "
                    f"{adv:+.0%}; if it rules for them, {fav:+.0%}.")

    if im:
        if edge >= 1.3:
            priced = (f"The options market expects only a ±{im:.1%} move — the verdict could move it "
                      f"{edge:.1f}× more. Not priced in yet.")
        elif edge >= 0.8:
            priced = f"The options market already expects about ±{im:.1%} — roughly in line, little edge."
        else:
            priced = f"The options market already expects ±{im:.1%} — more than the verdict's likely move."
    else:
        priced = "No options trade on this stock — it only matters if you hold the shares."

    # attention = likely size of move × how soon × (not priced in) × impact tier
    score = typical * 100 * urgency * (1.4 if edge and edge >= 1.3 else 1.0) *         {"material": 1.3, "near": 1.0}.get(tier, 0.6)
    priority = "high" if score >= 3 else "medium" if score >= 1.2 else "low"

    if decided:
        verdict = "Act now" if urgency >= 0.5 and typical >= 0.02 else "Low priority"
        why = ("The verdict is out — stocks often keep drifting for 1-2 weeks after. 5 min: read the "
               "verdict summary and decide." if verdict == "Act now" else "Old or small verdict — nothing to do.")
    elif kind == "verdict_soon" and priority != "low":
        verdict = "Act now"
        why = (f"The verdict is expected {when.split(' ', 1)[-1]}. 2 min: {hint}; if it's on, decide "
               f"before the market opens.")
    elif kind == "any_day" and priority != "low":
        verdict = "Prepare"
        why = f"Hearing is over, so the verdict can drop any morning — a {worst:.0%} move is possible. 2 min each evening: {hint}."
    elif kind == "hearing_soon" and priority != "low":
        verdict = "Prepare"
        why = (f"Hearing {when.split(' ', 1)[-1]} — usually no verdict yet, but it sets the next date. "
               f"2 min: {hint}.")
    elif priority == "high":
        verdict = "Prepare"
        why = f"A {worst:.0%} move is possible but there's no date yet. 5 min a week: {hint}."
    elif priority == "medium":
        verdict = "Watch"
        why = f"Moderate impact, no date yet. {hint[0].upper() + hint[1:]} — the reminder will tell you when."
    else:
        verdict = "Low priority"
        if kind in ("verdict_soon", "hearing_soon"):
            what = "verdict" if kind == "verdict_soon" else "hearing"
            why = (f"The {what} is {when.split(' ', 1)[-1]}, but the likely move is small "
                   f"(~{typical:.0%}) — optional to check.")
        elif kind == "any_day":
            why = f"Verdict could come any day, but the likely move is small (~{typical:.0%}) — optional."
        else:
            why = "Small likely move and nothing imminent — safe to ignore until the system flags a change."

    out.update(headline=headline, priced=priced, score=round(score, 2), priority=priority,
               verdict=verdict, why_time=why, worst=worst, typical=typical, edge=edge)
    return out
