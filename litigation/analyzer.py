"""
LLM analysis for the litigation tracker (Claude Sonnet by default — see
news_analyzer.llm; web search is used only where it's needed).

  discover_cases()  web: find material pending/recent proceedings for a company
  refresh_case()    web: latest stage of one known case (reserved? listed? decided?)
  classify_filing() no web: does a BSE filing create/update a material case?
  assess_case()     no web, point-in-time: P(adverse), move if adverse/favourable
  trade_idea()      deterministic: turns the assessment + live option prices into
                    a short-dated derivatives structure (or "no edge")
"""

from __future__ import annotations

import json
from datetime import date

from litigation.db import CLOSED_STAGES, HOT_STAGES, STAGES
from news_analyzer.llm import extract_json, generate
from utils.logger import get_logger

log = get_logger("litigation.analyzer")

_STAGE_LIST = " | ".join(STAGES)

_CASE_SCHEMA = f"""{{
  "title": "short name, e.g. 'AT1 bond write-off appeal'",
  "court": "Supreme Court | <State> High Court | NCLT | NCLAT | SAT | SEBI | CCI | ITAT | Arbitral tribunal | ...",
  "case_number": "official number if known, else null",
  "counterparty": "main opposing party",
  "company_role": "petitioner | respondent | appellant | defendant | noticee",
  "subject": "one line: what is being decided",
  "claim_cr": "₹ crore the LISTED COMPANY ITSELF could pay/lose/gain (number) or null — NOT claims against promoters personally, group entities or third parties",
  "stage": "{_STAGE_LIST}",
  "stage_date": "YYYY-MM-DD the current stage began, or null",
  "next_date": "YYYY-MM-DD next hearing / pronouncement date if known, or null",
  "listing_note": "where/how it is listed if known: bench, court no., item no., 'tentatively listed', or null",
  "date_source": "URL or source of next_date / stage_date (court page, LiveLaw report, filing), or null",
  "outcome": "if decided: 'adverse: ...' | 'favourable: ...' | 'mixed: ...'; else null",
  "key_facts": "2-4 sentences: lower-court rulings, stays, regulator stance, quantum vs capital",
  "sources": ["urls"]
}}"""

_DISCOVER_PROMPT = """\
Today is {today}. Company: {name} (NSE: {symbol}). Market cap ≈ ₹{mcap} crore.

Stock-exchange filings from this company that our keyword filter flagged as legal
(may be empty or noisy):
{filings}

TASK: Use web search (legal news such as LiveLaw, Bar & Bench, SC Observer; business
press; the company's annual-report contingent-liability and litigation notes) to
identify court, tribunal, arbitration or regulatory proceedings involving this
company that are PENDING, or were DECIDED in the last 30 days, and that could
plausibly move the share price by 3% or more. That means: claims/penalties of
roughly 1%+ of market cap, threats to a licence/plant/core business, capital or
solvency questions, regulator bans, promoter/governance actions.
IGNORE: routine recovery suits where the company is the creditor, consumer cases,
small tax demands, and matters already fully resolved more than 30 days ago.

Return ONLY this JSON (no prose):
{{"cases": [ {schema} ]}}
Return {{"cases": []}} if nothing material. Use null for unknowns — never guess
case numbers or dates.
"""

_REFRESH_PROMPT = """\
Today is {today}. Find the LATEST status of this legal proceeding using web search
(prefer LiveLaw, Bar & Bench, SC Observer, the court's own site, exchange filings).

Company: {name} (NSE: {symbol})
Case: {title} — {court} — {case_number}
Subject: {subject}
Last known stage: {stage} (since {stage_date}); next date: {next_date}

Key questions: Has judgment been reserved? Is it listed for pronouncement (and on
which date)? Has a ruling been delivered — if so, for or against the company's
shareholders? Any new hearing date, review petition, stay or settlement?

Return ONLY this JSON:
{{"stage": "{stages}", "stage_date": "YYYY-MM-DD or null",
  "next_date": "YYYY-MM-DD or null",
  "listing_note": "bench / court no. / item no. / 'tentatively listed' if known, or null",
  "date_source": "URL that states the date, or null",
  "outcome": "adverse: ... | favourable: ... | mixed: ... | null",
  "update": "one sentence on what changed (or 'no change')",
  "sources": ["urls"]}}
"""

_FILING_PROMPT = """\
A listed Indian company filed this with the stock exchange on {dt}.
Company: {name} (NSE: {symbol}), market cap ≈ ₹{mcap} crore.
Filing subject: {subject}
Filing text (truncated):
{text}

Does this filing disclose a NEW or UPDATED court / tribunal / regulatory proceeding
(order, judgment, penalty, demand, petition, show-cause) that could move the share
price by 2% or more? Routine compliance filings → not material.

Return ONLY JSON: {{"material": true|false, "case": {schema} }}
(set "case" to null when material is false)
"""

_ASSESS_PROMPT = """\
AS-OF DATE: {asof}. Use ONLY the facts below plus general legal and market
knowledge. Do NOT use any knowledge of events after the as-of date.

You are advising a derivatives trader on a pending legal event.
Company: {name} (NSE: {symbol})
Share price: ₹{price}   Market cap: ₹{mcap} crore   20-day realised vol: {hv}
Options-implied move to nearest expiry: {implied}

CASE FACTS:
{facts}

Estimate:
- p_adverse: probability (0-1) the ruling goes AGAINST the company's shareholders.
  Consider: lower-court/tribunal rulings and their reasoning, whether a stay was
  granted, how long it has been reserved, the regulator's/government's stance,
  how appellate courts treat such cases, and contract/statute text if known.
- move_if_adverse_pct / move_if_favourable_pct: signed % share-price reaction over
  the ruling day + the following week. Scale by money at stake vs market cap /
  net worth / regulatory capital, second-order effects (capital raise, dilution,
  licence loss), and how much the market seems to have priced in already.
- ruling_window: when a ruling is likely ("any day", "within 1 month", "3-6 months", "unknown").

Return ONLY JSON:
{{"p_adverse": 0.0, "move_if_adverse_pct": -0.0, "move_if_favourable_pct": 0.0,
  "ruling_window": "...", "confidence": "low|medium|high",
  "key_drivers": ["...", "..."], "rationale": "max 80 words"}}
"""


def _j(text: str) -> dict | None:
    v = extract_json(text)
    return v if isinstance(v, dict) else None


def _num(x):
    try:
        return None if x in (None, "", "null") else float(x)
    except (TypeError, ValueError):
        return None


def _norm_case(c: dict) -> dict:
    c = dict(c or {})
    st = str(c.get("stage") or "unknown").strip().lower().replace(" ", "_")
    c["stage"] = st if st in STAGES else "unknown"
    c["claim_cr"] = _num(c.get("claim_cr"))
    for k in ("stage_date", "next_date"):
        v = c.get(k)
        c[k] = v if isinstance(v, str) and len(v) >= 10 and v[:4].isdigit() else None
    if c.get("outcome") in ("null", "", "none"):
        c["outcome"] = None
    if isinstance(c.get("sources"), str):
        c["sources"] = [c["sources"]]
    return c


# ── LLM calls ─────────────────────────────────────────────────────────────────

def discover_cases(symbol: str, name: str, mcap_cr: float | None,
                   filings: list[dict] | None = None) -> list[dict]:
    fl = "\n".join(f"- {f.get('dt','')[:10]}: {f.get('subject','')[:200]} "
                   f"{(f.get('text') or '')[:400]}" for f in (filings or [])[:8]) or "(none)"
    prompt = _DISCOVER_PROMPT.format(today=date.today().isoformat(), name=name or symbol,
                                     symbol=symbol, mcap=f"{mcap_cr:,.0f}" if mcap_cr else "unknown",
                                     filings=fl, schema=_CASE_SCHEMA)
    try:
        out = generate(prompt, timeout=600, think=True, web=True, max_turns=16)
    except Exception as exc:
        log.warning(f"discover[{symbol}]: LLM failed — {exc}")
        return []
    d = _j(out) or {}
    cases = [_norm_case(c) for c in d.get("cases", []) if isinstance(c, dict) and c.get("title")]
    log.info(f"discover[{symbol}]: {len(cases)} case(s)")
    return cases


def refresh_case(case: dict, name: str = "") -> dict | None:
    prompt = _REFRESH_PROMPT.format(
        today=date.today().isoformat(), name=name or case["symbol"], symbol=case["symbol"],
        title=case.get("title"), court=case.get("court"), case_number=case.get("case_number") or "n/a",
        subject=case.get("subject"), stage=case.get("stage"), stage_date=case.get("stage_date"),
        next_date=case.get("next_date"), stages=_STAGE_LIST)
    try:
        out = generate(prompt, timeout=600, think=False, web=True, max_turns=10)
    except Exception as exc:
        log.warning(f"refresh[{case['symbol']}:{case.get('title')}]: LLM failed — {exc}")
        return None
    d = _j(out)
    return _norm_case({**case, **d}) | {"update": d.get("update", "")} if d else None


def classify_filing(f: dict, name: str, mcap_cr: float | None) -> dict | None:
    prompt = _FILING_PROMPT.format(dt=f.get("dt", "")[:10], name=name or f["symbol"],
                                   symbol=f["symbol"], mcap=f"{mcap_cr:,.0f}" if mcap_cr else "unknown",
                                   subject=f.get("subject", ""), text=(f.get("text") or "")[:6000],
                                   schema=_CASE_SCHEMA)
    try:
        d = _j(generate(prompt, timeout=300, think=False))
    except Exception as exc:
        log.warning(f"filing[{f['symbol']}]: LLM failed — {exc}")
        return None
    if not d or not d.get("material") or not isinstance(d.get("case"), dict):
        return None
    return _norm_case(d["case"])


def assess_case(case: dict, asof: str, name: str, price: float | None,
                mcap_cr: float | None, hv: float | None,
                implied_move: float | None) -> dict | None:
    facts = {k: case.get(k) for k in ("title", "court", "case_number", "counterparty",
                                      "company_role", "subject", "claim_cr", "stage",
                                      "stage_date", "next_date", "outcome", "key_facts")
             if case.get(k) not in (None, "")}
    prompt = _ASSESS_PROMPT.format(
        asof=asof, name=name or case.get("symbol"), symbol=case.get("symbol"),
        price=f"{price:.2f}" if price else "unknown",
        mcap=f"{mcap_cr:,.0f}" if mcap_cr else "unknown",
        hv=f"{hv:.0%}" if hv else "unknown",
        implied=f"±{implied_move:.1%}" if implied_move else "unknown (not in F&O or no data)",
        facts=json.dumps(facts, indent=1, ensure_ascii=False))
    try:
        d = _j(generate(prompt, timeout=600, think=True))
    except Exception as exc:
        log.warning(f"assess[{case.get('symbol')}]: LLM failed — {exc}")
        return None
    if not d:
        return None
    p = _num(d.get("p_adverse"))
    adv = _num(d.get("move_if_adverse_pct"))
    fav = _num(d.get("move_if_favourable_pct"))
    if p is None or adv is None or fav is None:
        return None
    p = min(max(p, 0.0), 1.0)
    # The model answers in percent; store fractions.
    adv, fav = adv / 100.0, fav / 100.0
    return {"p_adverse": p, "move_adverse": adv, "move_favourable": fav,
            "expected_abs_move": p * abs(adv) + (1 - p) * abs(fav),
            "expected_move": p * adv + (1 - p) * fav,
            "ruling_window": d.get("ruling_window"), "confidence": d.get("confidence"),
            "key_drivers": d.get("key_drivers") or [], "rationale": d.get("rationale", "")}


# ── Materiality + trade idea (deterministic) ──────────────────────────────────

def materiality(case: dict, mcap_cr: float | None) -> float | None:
    if case.get("claim_cr") and mcap_cr:
        return case["claim_cr"] / mcap_cr
    return None


def is_material(case: dict, a: dict | None, mcap_cr: float | None) -> bool:
    from config import LITIGATION_MATERIALITY, LITIGATION_MIN_MOVE
    ea = (a or {}).get("expected_abs_move") or 0.0
    if ea >= LITIGATION_MIN_MOVE:
        return True
    # A big claim only counts if the assessor also expects a real (≥2%) price move —
    # filters out e.g. large tax demands the market treats as routine, and claims the
    # extractor mis-attributed (promoter-personal, group-company).
    m = materiality(case, mcap_cr)
    return bool(m is not None and m >= LITIGATION_MATERIALITY and (a is None or ea >= 0.02))


def tier(case: dict) -> tuple[str, str]:
    """Colour tier for display, from a stored case row:
      "material" (red)   — passes is_material (alerts, trade box, signal gate)
      "near"     (amber) — within reach: expected |move| ≥ 60% of the move bar, or
                           claim ≥ half the mcap bar
      "below"    (grey)  — everything else
    Returns (tier, human-readable reason with the numbers vs thresholds)."""
    from config import LITIGATION_MATERIALITY as MAT, LITIGATION_MIN_MOVE as MV
    ea = case.get("expected_abs_move")
    m = case.get("materiality")
    a = None if case.get("p_adverse") is None else {"expected_abs_move": ea or 0.0}
    bits = [f"expected move {ea:.1%} (bar {MV:.0%})" if ea is not None else "not assessed"]
    if m is not None:
        bits.append(f"claim {m:.1%} of mcap (bar {MAT:.0%})")
    reason = " · ".join(bits)
    if is_material(case, a, case.get("mcap_cr")):
        return "material", reason
    if (ea or 0) >= 0.6 * MV or (m is not None and m >= 0.5 * MAT):
        return "near", reason
    return "below", reason


def trade_idea(case: dict, a: dict | None, snap: dict | None, lot: int | None) -> str:
    """Turn an assessment into a short-dated derivatives idea — or say there's no
    edge. Rules (all thresholds are on the ruling-window move, not annualised):
      • ruling not imminent            → watch only (don't bleed premium for months)
      • P(adverse) ≥ 0.65 & big move   → buy puts (ATM / 1 strike OTM)
      • P(adverse) ≤ 0.35 & big move   → buy calls
      • our |move| ≥ 1.3× implied move → long straddle (direction unclear, vol cheap)
      • otherwise                      → no edge: options already price it
    """
    stage = case.get("stage")
    if stage in CLOSED_STAGES:
        out = (case.get("outcome") or "").lower()
        if out.startswith("adverse"):
            return ("RULING OUT (adverse). Post-ruling drift play: short futures / buy "
                    "puts at the next open, 1-2 week horizon, stop above the ruling-day "
                    "high — adverse rulings often keep drifting as analysts re-rate capital.")
        if out.startswith("favourable"):
            return "RULING OUT (favourable). Relief move — only chase if it closes strong on day 1."
        return "Ruling delivered — re-assess on the full judgment text."
    if not a:
        return "Watch — not yet assessed."
    if stage not in HOT_STAGES:
        return ("Watch — no ruling imminent. Act when judgment is RESERVED / LISTED FOR "
                "PRONOUNCEMENT; buying options now just bleeds premium.")
    p, adv, fav = a["p_adverse"], a["move_adverse"], a["move_favourable"]
    ea = a["expected_abs_move"]
    if not snap:
        side = "trim / avoid fresh longs" if p >= 0.5 else "avoid fresh shorts"
        return (f"Not in F&O (or no option data). Cash only: {side} into the ruling; "
                f"expected |move| {ea:.0%}.")
    im = snap["implied_move"]
    k, exp = snap["strike"], snap["expiry"]
    lot_txt = lambda prem: f" ≈ ₹{prem * lot:,.0f}/lot" if lot else ""
    when = ("Enter when the case is LISTED FOR PRONOUNCEMENT (day before); "
            if stage != "listed_for_pronouncement" else "Listed — enter at next open; ")
    if p >= 0.65 and abs(adv) >= max(1.5 * im, 0.05):
        return (f"{when}BUY PUT {k:g} PE {exp} @≈₹{snap['put']:.2f}{lot_txt(snap['put'])}. "
                f"P(adverse) {p:.0%}, if adverse {adv:+.0%} vs options pricing ±{im:.1%}. "
                f"Max loss = premium.")
    if p <= 0.35 and fav >= max(1.5 * im, 0.05):
        return (f"{when}BUY CALL {k:g} CE {exp} @≈₹{snap['call']:.2f}{lot_txt(snap['call'])}. "
                f"P(favourable) {1-p:.0%}, if favourable {fav:+.0%} vs ±{im:.1%} priced.")
    if ea >= 1.3 * im:
        return (f"{when}LONG STRADDLE {k:g} {exp} @≈₹{snap['straddle']:.2f}"
                f"{lot_txt(snap['straddle'])} — direction unclear (P adverse {p:.0%}) but our "
                f"expected |move| {ea:.0%} vs ±{im:.1%} priced.")
    return (f"No edge — options already price ±{im:.1%} vs our expected |move| {ea:.0%}. "
            f"Skip, or trim stock exposure.")
