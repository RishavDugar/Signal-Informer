"""
Where the data lives — official court / tribunal / regulator pages for each case,
plus how to find the case there.

Most case-status and cause-list pages are captcha-gated, so we never scrape them:
we deep-link the exact page and say what to type in. The user downloads or
screenshots the result and attaches it on the dashboard (litigation.attachments),
where Claude reads it.

Exception: NCLAT posts its daily cause lists as plain PDF links, which
auto_cause_lists() fetches so NCLAT cases are checked automatically.

URLs verified 2026-11-18. Sites marked `browser_only` time out or have SSL-chain
problems for scripts but open normally in a browser.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from urllib.parse import quote_plus

from utils.logger import get_logger

log = get_logger("litigation.portals")

# Each portal: list of (label, url, hint). Hints say what to enter on the page.
_SC = [
    ("SC: find case by number", "https://www.sci.gov.in/case-status-case-no/",
     "Enter case type, number and year, solve the captcha, then save the page as PDF and attach it"),
    ("SC: find case by filing number", "https://www.sci.gov.in/case-status-diary-no/",
     "Shows the next likely hearing date ('Tentatively listed on')"),
    ("SC: find case by company name", "https://www.sci.gov.in/case-status-party-name/",
     "Type the company name, choose 'pending'"),
    ("SC: tomorrow's court schedule", "https://www.sci.gov.in/cause-list/",
     "Usually online by evening. If the case is in the 'FOR JUDGMENT' section, the verdict comes tomorrow"),
    ("SC: hearing notes", "https://www.sci.gov.in/daily-order-case-no/",
     "What happened at a hearing and the next date"),
    ("SC: verdicts", "https://www.sci.gov.in/judgements-case-no/", "The verdict PDF, once uploaded"),
    ("SC: all verdicts on a date", "https://www.sci.gov.in/judgements-judgement-date/", "Every verdict given on a day"),
    ("SC: registry note", "https://www.sci.gov.in/office-report-case-no/", "Court office note before a hearing"),
]
_HC_COMMON = [
    ("High Courts: case page & schedule", "https://hcservices.ecourts.gov.in/hcservices/main.php",
     "Pick the High Court, then Case Status (case page), Cause List (schedule) or Orders (hearing notes)"),
    ("High Courts: search verdicts", "https://judgments.ecourts.gov.in/pdfsearch/index.php",
     "Search verdict text by company name"),
]
_HC_SITES = {
    "bombay": ("Bombay HC site", "https://bombayhighcourt.nic.in/", True),
    "delhi": ("Delhi HC site", "https://delhihighcourt.nic.in/web/", False),
    "madras": ("Madras HC site", "https://www.mhc.tn.gov.in/", True),
    "gujarat": ("Gujarat HC site", "https://gujarathighcourt.nic.in/", False),
    "karnataka": ("Karnataka HC site", "https://karnatakajudiciary.kar.nic.in/", True),
    "calcutta": ("Calcutta HC site", "https://www.calcuttahighcourt.gov.in/", True),
}
_NCLT = [
    ("NCLT: court schedule", "https://nclt.gov.in/cause-list", "Pick the city bench and date"),
    ("NCLT: case page", "https://nclt.gov.in/", "Case Status → company name → captcha"),
]
_NCLAT = [
    ("NCLAT: court schedule", "https://nclat.nic.in/daily-cause-list", "Checked automatically every night"),
    ("NCLAT: case page", "https://nclat.nic.in/display-board/cases", "Search by company name"),
    ("NCLAT: hearing notes", "https://nclat.nic.in/display-board/orders", "What happened at a hearing"),
    ("NCLAT: verdicts", "https://nclat.nic.in/display-board/judge", "Final verdict PDFs"),
]
_SAT = [("SAT (Securities Appellate Tribunal)", "https://sat.gov.in/", "Schedule, hearing notes, verdicts")]
_SEBI = [("SEBI: orders against companies", "https://www.sebi.gov.in/enforcement/orders/", "Filter by company name")]
_CCI = [("CCI: penalty orders", "https://www.cci.gov.in/antitrust/orders", "Search by company name"),
        ("CCI home", "https://www.cci.gov.in/", "")]
_TDSAT = [("TDSAT: case page", "https://tdsat.gov.in/Delhi/services/casestatus.php?chek=1234", ""),
          ("TDSAT: court schedule", "https://tdsat.gov.in/Delhi/services/causelistent.php?chek=1234", ""),
          ("TDSAT: hearing notes", "https://tdsat.gov.in/Delhi/services/dailyorderlist.php", ""),
          ("TDSAT: verdicts", "https://tdsat.gov.in/Delhi/services/judgment.php", "")]
_CESTAT = [("CESTAT: hearing notes", "https://cestat.gov.in/order-status", ""),
           ("CESTAT: verdicts", "https://cestat.gov.in/final-order-status-all", "")]
_ITAT = [("ITAT", "https://itat.gov.in/", "Case page and verdicts")]
_APTEL = [("APTEL", "https://aptel.gov.in/", "Schedule and verdicts")]
_DRT = [("DRT: case page", "https://drt.gov.in/", "Pick the city bench")]
_NGT = [("NGT: schedule & orders", "https://www.greentribunal.gov.in/", "")]
_DISTRICT = [("District courts: case page", "https://services.ecourts.gov.in/ecourtindia_v6/", "Search by company name → captcha")]


def official_links(case: dict) -> list[dict]:
    """[{label, url, hint}] for the court/regulator handling this case."""
    court = (case.get("court") or "").lower()
    out: list[tuple] = []
    if "supreme" in court:
        out += _SC
    elif "nclat" in court or "appellate tribunal" in court and "company" in court:
        out += _NCLAT
    elif "nclt" in court or "company law" in court:
        out += _NCLT
    elif "securities appellate" in court or re.search(r"\bsat\b", court):
        out += _SAT
    elif "sebi" in court:
        out += _SEBI + _SAT
    elif "competition" in court or re.search(r"\bcci\b", court):
        out += _CCI + _NCLAT
    elif "tdsat" in court or "telecom" in court:
        out += _TDSAT
    elif "cestat" in court or "customs" in court:
        out += _CESTAT
    elif "itat" in court or "income tax" in court or re.search(r"\bcit\b|cit\s*\(a", court):
        out += _ITAT
    elif "aptel" in court or "electricity" in court:
        out += _APTEL
    elif "drt" in court or "debt recovery" in court:
        out += _DRT
    elif "green" in court or "ngt" in court:
        out += _NGT
    elif "high court" in court or re.search(r"\bhc\b", court):
        for key, (label, url, browser_only) in _HC_SITES.items():
            if key in court:
                out.append((label, url, "Case page, court schedule, hearing notes" +
                            (" — browser only" if browser_only else "")))
        out += _HC_COMMON
    elif "district" in court or "civil court" in court or "magistrate" in court:
        out += _DISTRICT
    elif "arbitra" in court or "siac" in court or "lcia" in court:
        out += [("Enforcement usually runs via the High Court", _HC_COMMON[0][1],
                 "Search the enforcement petition in the HC with jurisdiction")]
    links = [{"label": l, "url": u, "hint": h, "kind": "official"} for l, u, h in out]
    links += company_filing_links(case)
    links += news_links(case)
    return links


def company_filing_links(case: dict) -> list[dict]:
    """The company's own exchange filings — listed companies must disclose material
    litigation outcomes, tax demands and regulatory orders here (Reg 30)."""
    try:
        from litigation.sources import bse_scrips
        info = bse_scrips().get((case.get("symbol") or "").upper(), {})
    except Exception:
        info = {}
    out = []
    if info.get("scrip"):
        out.append({"label": "Company filings (BSE)", "kind": "official",
                    "url": f"https://www.bseindia.com/corporates/ann.html?scrip={info['scrip']}",
                    "hint": "What the company itself told the exchange about the case"})
    sym = case.get("symbol")
    if sym:
        out.append({"label": "Company filings (NSE)", "kind": "official",
                    "url": f"https://www.nseindia.com/get-quotes/equity?symbol={quote_plus(sym)}",
                    "hint": "Company announcements"})
    return out


def news_links(case: dict) -> list[dict]:
    """Legal-news searches — free, no captcha, usually the first to report
    'reserved' / 'listed for pronouncement' / the ruling itself."""
    name = case.get("company_name") or case.get("symbol") or ""
    topic = case.get("title") or case.get("subject") or ""
    q = f"{name} {topic}".strip()
    return [
        {"label": "Google News (last week)", "kind": "news",
         "url": f"https://news.google.com/search?q={quote_plus(q + ' when:7d')}&hl=en-IN&gl=IN&ceid=IN:en",
         "hint": "Usually the first to report hearing dates and verdicts"},
        {"label": "LiveLaw search", "kind": "news",
         "url": f"https://www.livelaw.in/search?q={quote_plus(q)}", "hint": ""},
        {"label": "Bar & Bench search", "kind": "news",
         "url": f"https://www.barandbench.com/search?q={quote_plus(q)}", "hint": ""},
    ]


# ── NCLAT: cause-list PDFs are public links → automatic scanning ──────────────

def nclat_cause_lists(for_dates: list[date] | None = None) -> list[dict]:
    """[{date, label, url}] for NCLAT cause-list PDFs linked on its homepage."""
    from litigation.sources import _get
    r = _get("https://nclat.nic.in/", {"Referer": "https://nclat.nic.in/"}, tries=2)
    if r is None:
        return []
    want = {d.strftime("%d.%m.%Y") for d in (for_dates or [])}
    out = []
    for href, txt in re.findall(r'href="([^"]+\.pdf)"[^>]*>\s*([^<]*Cause List[^<]*)<', r.text, re.I):
        m = re.search(r"(\d{2}\.\d{2}\.\d{4})", txt)
        if not m or (want and m.group(1) not in want):
            continue
        url = href if href.startswith("http") else "https://nclat.nic.in" + href
        out.append({"date": datetime.strptime(m.group(1), "%d.%m.%Y").date().isoformat(),
                    "label": "NCLAT " + " ".join(txt.split()), "url": url})
    return out


def sc_cause_lists(from_date: date, to_date: date) -> list[dict]:
    """Supreme Court daily + advance cause lists — public PDFs on api.sci.gov.in,
    linked from sci.gov.in/cause-list ('Cause Lists at a Glance'); no captcha.
    Verdicts to be delivered sit under '[ JUDGMENT BY :- HON'BLE ... ]'."""
    from litigation.sources import _get
    r = _get("https://www.sci.gov.in/cause-list/", {"Referer": "https://www.sci.gov.in/"}, tries=2, timeout=60)
    if r is None:
        return []
    out, seen = [], set()
    for href in re.findall(r'href="(https://api\.sci\.gov\.in/jonew/cl/(?:advance/)?\d{4}-\d{2}-\d{2}/[^"]+\.pdf)"', r.text):
        m = re.search(r"/cl/(advance/)?(\d{4}-\d{2}-\d{2})/([^/]+)\.pdf", href)
        d = date.fromisoformat(m.group(2))
        if href in seen or not (from_date <= d <= to_date):
            continue
        seen.add(href)
        kind = "advance list" if m.group(1) else "daily list"
        out.append({"date": d.isoformat(), "url": href, "court": "Supreme Court",
                    "label": f"Supreme Court {kind} {d:%d %b} ({m.group(3)})"})
    return out


def dhc_cause_lists(for_dates: list[date]) -> list[dict]:
    """Delhi High Court cause lists — public PDFs, no captcha. 'j_DDMMYYYY' is the
    judgments list; 'c_' / 'finals_' / 'combined_advance_' the hearing lists."""
    from litigation.sources import _get
    r = _get("https://delhihighcourt.nic.in/web/cause-lists/cause-list",
             {"Referer": "https://delhihighcourt.nic.in/"}, tries=2, timeout=60)
    if r is None:
        return []
    want = {d.strftime("%d%m%Y"): d for d in for_dates} | {d.strftime("%d.%m.%Y"): d for d in for_dates}
    out = []
    for href in sorted(set(re.findall(r'href="(/files/[^"]+\.pdf)"', r.text))):
        name = href.rsplit("/", 1)[-1]
        if not re.match(r"(j_|c_|new_c_|finals_|combined_advance_|combined_targetted_)", name):
            continue
        d = next((v for k, v in want.items() if k in name), None)
        if d:
            out.append({"date": d.isoformat(), "url": "https://delhihighcourt.nic.in" + href,
                        "court": "Delhi High Court", "label": f"Delhi HC list {d:%d %b} ({name})"})
    return out


def auto_cause_lists() -> list[dict]:
    """Every public, captcha-free cause list worth scanning tonight:
    Supreme Court (daily lists for today/next working day + advance lists up to a
    week ahead), Delhi High Court and NCLAT (today + next working day)."""
    today = date.today()
    nxt = today + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    lists = []
    for fn in (lambda: sc_cause_lists(today, today + timedelta(days=8)),
               lambda: dhc_cause_lists([today, nxt]),
               lambda: [dict(x, court="NCLAT") for x in nclat_cause_lists([today, nxt])]):
        try:
            lists += fn()
        except Exception as exc:
            log.warning(f"auto_cause_lists: a source failed — {exc}")
    return lists
