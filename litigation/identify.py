"""
Search recipes — exactly what to type on the court website to find a case.

Party-name search on court sites is loose: "IDEA" returns every Vodafone Idea tax
file. So for each tracked case Claude (web search) finds the case's official
identifiers — case type as it appears in the court's dropdown, number, year,
diary (filing) number, cause title, which side the company is on — and we turn
them into a fill-in-the-boxes recipe per court:

  steps     page link + the exact field values to enter (best route first)
  spot      how to recognise the right row among similar results
  look_for  which fields on the result page answer our question, in plain English

Stored as JSON in lit_cases.search_json. Re-run any time: python -m litigation.identify
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from litigation import db as ldb
from litigation.analyzer import tier
from news_analyzer.llm import extract_json, generate
from utils.logger import get_logger

log = get_logger("litigation.identify")

# Exact options in the Supreme Court "Case Number" form (read from sci.gov.in, Nov 2026).
SC_CASE_TYPES = [
    "SPECIAL LEAVE PETITION (CIVIL)", "SPECIAL LEAVE PETITION (CRIMINAL)", "CIVIL APPEAL", "CRIMINAL APPEAL",
    "WRIT PETITION (CIVIL)", "WRIT PETITION(CRIMINAL)", "TRANSFER PETITION (CIVIL)", "REVIEW PETITION (CIVIL)",
    "TRANSFERRED CASE (CIVIL)", "ORIGINAL SUIT", "CONTEMPT PETITION (CIVIL)", "TAX REFERENCE CASE",
    "ARBITRATION PETITION", "CURATIVE PETITION(CIVIL)", "MISCELLANEOUS APPLICATION",
]

_PROMPT = """\
Today is {today}. Find the OFFICIAL court identifiers for this case so a person can look it
up on the court's website. Use web search: LiveLaw, Bar & Bench, SC Observer, Indian Kanoon,
court orders/judgments, the company's stock-exchange disclosures and annual report.

CASE WE TRACK
Company: {company} (NSE: {symbol})
Title: {title}
Court / forum: {court}
Known case number (may be wrong or empty): {case_number}
Counterparty: {counterparty}
Subject: {subject}

Return ONLY JSON:
{{
  "forum": "the exact forum, e.g. 'Supreme Court of India', 'Bombay High Court (Principal Seat)', 'NCLT Mumbai Bench', 'NCLAT New Delhi', 'Securities Appellate Tribunal', 'CCI', 'ITAT Mumbai'",
  "case_type": "case type exactly as the court lists it. For the Supreme Court use one of: {sc_types}. For High Courts the eCourts abbreviation (e.g. 'WP', 'LPA', 'COMAP', 'ARBP', 'COMS'). For NCLT/NCLAT e.g. 'CP(IB)', 'Company Appeal (AT) (Insolvency)'. null if unknown",
  "case_number": "number only, e.g. '3713', or null",
  "case_year": "YYYY or null",
  "diary_number": "Supreme Court diary (filing) number, or null",
  "diary_year": "YYYY or null",
  "connected": "other case numbers heard together (tagged / connected matters), or null",
  "cause_title": "as it appears in court, e.g. 'Vodafone Idea Ltd. v. Union of India'",
  "petitioner": "first petitioner/appellant exactly as named in court records",
  "respondent": "first respondent exactly as named in court records",
  "company_side": "petitioner | respondent",
  "registered_name": "the company's name as it appears in court records (often older/legal name, e.g. 'IDEA CELLULAR LTD' before 2018)",
  "listed_as": ["other party names the court lists this case under when the company itself is NOT named — its subsidiary / SPV / the acquired target, e.g. 'Delhi Airport Metro Express Pvt Ltd' for Reliance Infrastructure, 'Bhushan Power and Steel Ltd' for JSW Steel. Never the opposing party. [] if none"],
  "filed_year": "YYYY the case was filed",
  "last_hearing": "YYYY-MM-DD or null",
  "next_date": "YYYY-MM-DD or null",
  "stage_now": "plain words: e.g. 'hearing over, verdict pending'",
  "still_pending": "true if the case is still pending before a court/tribunal; false if disposed/withdrawn/sent back to the government or regulator; null if unclear",
  "where_now": "if not pending in court: where the matter sits now (e.g. 'with DoT for reassessment'), else null",
  "status_confidence": "high | medium | low — how sure you are about still_pending (high only from a court order or two reliable reports)",
  "distinguish": "one line: what makes this case different from the company's other cases in the same court (subject, other party, act/section)",
  "confidence": "high | medium | low  (high only if the case number comes from an official source or a court order)",
  "sources": ["urls where you found the identifiers"]
}}
Never invent numbers: use null when unsure. A wrong case number is worse than none.
"""


def _find_ids(c: dict) -> dict | None:
    from litigation.sources import company_info
    company = c.get("company_name") or company_info(c["symbol"]).get("name") or c["symbol"]
    p = _PROMPT.format(today=datetime.now().date().isoformat(), company=company, symbol=c["symbol"],
                       title=c.get("title"), court=c.get("court"), case_number=c.get("case_number") or "—",
                       counterparty=c.get("counterparty") or "—", subject=c.get("subject") or "—",
                       sc_types=", ".join(f"'{t}'" for t in SC_CASE_TYPES))
    try:
        d = extract_json(generate(p, timeout=600, think=True, web=True, max_turns=16))
    except Exception as exc:
        log.warning(f"identify[{c['symbol']}#{c['id']}]: {exc}")
        return None
    return d if isinstance(d, dict) else None


# ── Recipe builder (deterministic, per forum) ─────────────────────────────────

def _v(x) -> str | None:
    x = (str(x).strip() if x is not None else "")
    return None if not x or x.lower() in ("null", "none", "unknown", "n/a", "—") else x


def _short_party(name: str | None) -> str | None:
    """Most distinctive part of a party name for 'contains' searches:
    'VODAFONE IDEA LIMITED' → 'VODAFONE IDEA'."""
    n = _v(name)
    if not n:
        return None
    import re
    n = re.sub(r"\b(limited|ltd\.?|private|pvt\.?|and others|& ors\.?|& anr\.?|thr\.?.*)$", "", n,
               flags=re.I).strip(" .,")
    return n.upper()


def build_recipe(c: dict, ids: dict) -> dict:
    forum = (_v(ids.get("forum")) or c.get("court") or "").lower()
    ctype, num, yr = _v(ids.get("case_type")), _v(ids.get("case_number")), _v(ids.get("case_year"))
    dno, dyr = _v(ids.get("diary_number")), _v(ids.get("diary_year"))
    side = (_v(ids.get("company_side")) or "").lower()
    me = _short_party(ids.get("registered_name")) or _short_party(c.get("company_name")) or c["symbol"]
    other = _v(ids.get("respondent") if side == "petitioner" else ids.get("petitioner")) or _v(c.get("counterparty"))
    fy = _v(ids.get("filed_year"))
    steps: list[dict] = []
    look_for: list[str] = []

    if "supreme" in forum:
        if ctype and num and yr:
            steps.append({"route": "By case number (most reliable)", "page": "SC: find case by number",
                          "url": "https://www.sci.gov.in/case-status-case-no/",
                          "fill": [["Select case type", ctype], ["Case Number", num], ["Case Year", yr]]})
        if dno and dyr:
            steps.append({"route": "By filing (diary) number", "page": "SC: find case by filing number",
                          "url": "https://www.sci.gov.in/case-status-diary-no/",
                          "fill": [["Diary Number", dno], ["Diary year", dyr]]})
        steps.append({"route": "By company name (if the numbers don't work)", "page": "SC: find case by company name",
                      "url": "https://www.sci.gov.in/case-status-party-name/",
                      "fill": [["Petitioner / Respondent", "Petitioner" if side == "petitioner" else
                                "Respondent" if side == "respondent" else "Any"],
                               ["Party's Name", me], ["Diary Year", fy or "year the case was filed"],
                               ["Status", "Pending"]]})
        look_for = ["'Status/Stage' — Pending or Disposed, and the stage in brackets",
                    "'Tentatively listed on' — the next likely hearing date (this is the date we need)",
                    "'Listing Dates' tab — past and upcoming hearing dates",
                    "'Judgments/Orders' tab — open the latest order PDF: it says 'reserved', 'list on <date>' or the verdict. Save it and 📎 Attach."]
    elif "high court" in forum or forum.endswith(" hc"):
        bench = _v(ids.get("forum")) or c.get("court")
        if ctype and num and yr:
            steps.append({"route": "By case number", "page": "High Courts: case page & schedule",
                          "url": "https://hcservices.ecourts.gov.in/hcservices/main.php",
                          "fill": [["Select State / High Court", bench], ["Menu", "Case Status → Case Number"],
                                   ["Case Type", ctype], ["Case Number", num], ["Year", yr]]})
        steps.append({"route": "By company name", "page": "High Courts: case page & schedule",
                      "url": "https://hcservices.ecourts.gov.in/hcservices/main.php",
                      "fill": [["Select State / High Court", bench], ["Menu", "Case Status → Party Name"],
                               ["Petitioner/Respondent", me], ["Registration Year", fy or "year filed"],
                               ["Status", "Pending"]]})
        look_for = ["'Next Hearing Date' — the date we need",
                    "'Stage of Case' — e.g. 'Admission', 'Final hearing', 'Judgment reserved'",
                    "'Orders' section — open the latest order and 📎 Attach it"]
    elif "nclat" in forum:
        steps.append({"route": "By company name / appeal number", "page": "NCLAT: case page",
                      "url": "https://nclat.nic.in/display-board/cases",
                      "fill": [["Search", f"{ctype or 'Company Appeal (AT)'} {num or ''}/{yr or ''}".strip(" /")
                                if num else me]]})
        look_for = ["'Next Date of Hearing'", "'Status' — Pending / Disposed / Reserved",
                    "Daily orders → latest order PDF → 📎 Attach"]
    elif "nclt" in forum:
        steps.append({"route": "By company name", "page": "NCLT: case page", "url": "https://nclt.gov.in/",
                      "fill": [["Menu", "Case Status"], ["Bench", _v(ids.get("forum")) or "the city bench"],
                               ["Party name", me],
                               ["Case number (if asked)", f"{ctype or 'CP(IB)'} {num}/{yr}" if num else "—"]]})
        look_for = ["'Next Hearing Date'", "'Case Status'", "Latest order PDF → 📎 Attach"]
    elif "appellate tribunal" in forum and "securit" in forum or "sat" == forum.strip():
        steps.append({"route": "SAT website", "page": "SAT (Securities Appellate Tribunal)", "url": "https://sat.gov.in/",
                      "fill": [["Search", f"Appeal No. {num} of {yr}" if num else me]]})
        look_for = ["Next date / status", "Latest order → 📎 Attach"]
    elif "sebi" in forum:
        steps.append({"route": "SEBI orders list", "page": "SEBI: orders against companies",
                      "url": "https://www.sebi.gov.in/enforcement/orders/", "fill": [["Search / filter", me]]})
        look_for = ["Newest order about this matter (date + whether it is final or interim)"]
    elif "competition" in forum or "cci" in forum:
        steps.append({"route": "CCI orders", "page": "CCI: penalty orders", "url": "https://www.cci.gov.in/antitrust/orders",
                      "fill": [["Search", f"Case No. {num} of {yr}" if num else me]]})
        look_for = ["Final order / penalty amount", "Appeals go to NCLAT — check there for the next date"]
    else:
        look_for = ["Next hearing date", "Current stage", "Latest order"]

    if not steps:
        steps.append({"route": "Company's own disclosures (tax / regulator matters aren't public)",
                      "page": "Company filings (BSE)", "url": None,
                      "fill": [["Look for", "Regulation 30 updates mentioning this demand / order"]]})

    spot = []
    if _v(ids.get("cause_title")):
        spot.append(f"Cause title should read like: {ids['cause_title']}")
    if other:
        spot.append(f"Other side: {other}")
    if _v(ids.get("distinguish")):
        spot.append(ids["distinguish"])
    if _v(ids.get("connected")):
        spot.append(f"Heard together with: {ids['connected']}")
    return {"steps": steps, "spot": spot, "look_for": look_for,
            "ids": {k: _v(ids.get(k)) for k in ("forum", "case_type", "case_number", "case_year",
                                                  "diary_number", "diary_year", "cause_title",
                                                  "registered_name", "company_side", "filed_year",
                                                  "last_hearing", "next_date", "stage_now", "connected")},
            "listed_as": [x for x in (ids.get("listed_as") or []) if isinstance(x, str) and x.strip()][:4],
            "confidence": _v(ids.get("confidence")) or "low",
            "sources": [u for u in (ids.get("sources") or []) if isinstance(u, str) and u.startswith("http")][:5],
            "updated": datetime.now().isoformat(timespec="seconds")}


def _targets(force: bool = False) -> list[dict]:
    rows = ldb.query("SELECT * FROM lit_cases WHERE status IN ('active','immaterial') "
                     "AND stage NOT IN ('decided','disposed')")
    out = []
    for c in rows:
        t = tier(c)[0]
        if t in ("material", "near") or c.get("stage") in ldb.HOT_STAGES or c.get("next_date"):
            if force or not c.get("search_json"):
                out.append(c)
    return out


def identify(case_ids: list[int] | None = None, force: bool = False) -> int:
    ldb.init()
    todo = [c for c in _targets(force) if not case_ids or c["id"] in case_ids]
    log.info(f"identify: {len(todo)} case(s)")
    n = 0

    def _one(c):
        return c, _find_ids(c)

    with ThreadPoolExecutor(max_workers=3) as ex:
        for fut in as_completed([ex.submit(_one, c) for c in todo]):
            c, ids = fut.result()
            if not ids:
                continue
            rec = build_recipe(c, ids)
            upd = {"search_json": json.dumps(rec, ensure_ascii=False)}
            i = rec["ids"]
            # Fill in official identifiers we were missing (only from a confident source)
            if rec["confidence"] in ("high", "medium") and i.get("case_number") and not c.get("case_number"):
                upd["case_number"] = " ".join(x for x in (i.get("case_type"), i.get("case_number") and
                                                          f"{i['case_number']}/{i.get('case_year') or ''}".rstrip("/")) if x)
            if rec["confidence"] in ("high", "medium") and i.get("next_date") and i["next_date"] >= datetime.now().date().isoformat() \
                    and i["next_date"] != c.get("next_date"):
                upd["next_date"] = i["next_date"]
                upd["date_source"] = (rec["sources"] or ["web search"])[0]
            # Case no longer in court (disposed / sent back to govt) → close it, say where it went
            sp, sc = ids.get("still_pending"), _v(ids.get("status_confidence")) or "low"
            if (sp is False or str(sp).lower() == "false") and sc in ("high", "medium"):
                where = _v(ids.get("where_now")) or "no longer before the court"
                upd.update(stage="disposed", status="closed",
                           outcome=f"not pending in court — {where}",
                           stage_date=_v(ids.get("last_hearing")) or c.get("stage_date"))
                ldb.add_event(c["id"], c["symbol"], "stage_change", c.get("stage"), "disposed",
                              f"Not pending in court: {where}")
            sets = ", ".join(f"{k}=?" for k in upd)
            ldb.execute(f"UPDATE lit_cases SET {sets} WHERE id=?", (*upd.values(), c["id"]))
            if "next_date" in upd:
                ldb.add_event(c["id"], c["symbol"], "date_change", c.get("next_date"), upd["next_date"],
                              "found while building the search recipe")
            n += 1
            log.info(f"identify[{c['symbol']}#{c['id']}]: {rec['confidence']} — "
                     f"{i.get('case_type')} {i.get('case_number')}/{i.get('case_year')} diary {i.get('diary_number')}")
    return n


if __name__ == "__main__":
    from data.db import init_db
    init_db()
    ids = [int(a) for a in sys.argv[1:] if a.isdigit()]
    print(f"identify: {identify(ids or None, force='--force' in sys.argv)} recipe(s) built")
