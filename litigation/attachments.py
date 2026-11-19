"""
User-attached documents → Claude analysis → case update.

You download a captcha-gated page (case status, cause list, daily order,
judgment) as PDF / HTML / screenshot and attach it on the dashboard:

  analyze_for_case(case_id, file)  one case — what does this document say about it?
  scan_document(file)              a whole cause list / order list — which of the
                                   tracked cases appear in it, and what does it mean?

Long documents (a day's cause list runs to hundreds of pages) are first narrowed
to the passages that mention the case (case number, company, counterparty), so
Claude reads only what matters. Short documents and images (screenshots, scanned
PDFs) are opened by Claude directly via its Read tool.

Results update the case (stage / next date / outcome / listing details), log an
event (→ WhatsApp alert if material), re-assess the impact, mark that case's
pending checks done, and are kept with the document on the case card.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from config import BASE_DIR
from litigation import db as ldb
from litigation.db import STAGES
from news_analyzer.llm import extract_json, generate
from utils.logger import get_logger

log = get_logger("litigation.attachments")

UPLOAD_DIR = BASE_DIR / "db" / "litigation_uploads"
ALLOWED = {".pdf", ".html", ".htm", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".csv", ".mhtml"}
_IMAGE = {".png", ".jpg", ".jpeg", ".webp"}
_INLINE_LIMIT = 40000        # chars of extracted text sent inline before narrowing


# ── Storage + text extraction ─────────────────────────────────────────────────

def save_upload(filename: str, data: bytes, case_id: int | None) -> Path:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED:
        raise ValueError(f"unsupported file type {ext} — use PDF, HTML, TXT or an image")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)[-80:]
    d = UPLOAD_DIR / (str(case_id) if case_id else "general")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{datetime.now():%Y%m%d_%H%M%S}_{safe}"
    p.write_bytes(data)
    return p


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            try:                                  # PyMuPDF: ~100x faster on 300-page cause lists
                import pymupdf
                with pymupdf.open(str(path)) as doc:
                    return "\n".join(f"[page {i + 1}]\n{pg.get_text()}" for i, pg in enumerate(doc))
            except ImportError:
                from pypdf import PdfReader
                rd = PdfReader(str(path))
                return "\n".join(f"[page {i + 1}]\n{pg.extract_text() or ''}" for i, pg in enumerate(rd.pages))
        if ext in (".html", ".htm", ".mhtml"):
            from bs4 import BeautifulSoup
            raw = path.read_bytes().decode("utf-8", errors="replace")
            return BeautifulSoup(raw, "html.parser").get_text("\n")
        if ext in (".txt", ".csv"):
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        log.warning(f"extract_text({path.name}) failed — {exc}")
    return ""                                   # images / scanned PDFs → Claude reads the file


# ── Finding a case inside a long document ─────────────────────────────────────

_GENERIC = {"limited", "ltd", "india", "indian", "private", "pvt", "company", "corporation",
            "the", "and", "of", "co", "industries", "bank", "union", "state", "others", "anr", "ors"}


def _search(case: dict) -> dict:
    s = case.get("search")
    if s is None and case.get("search_json"):
        try:
            s = json.loads(case["search_json"])
        except Exception:
            s = None
    return s or {}


def _ids(case: dict) -> dict:
    return _search(case).get("ids", {}) or {}


def _terms(case: dict) -> list[re.Pattern]:
    """Case number(s) (strongest) + every name the court may list the case under:
    the company's registered / current name and any subsidiary / target it is
    listed as ('listed_as'). The opposing party is deliberately NOT a search term —
    'Union of India' or a regulator appears in hundreds of unrelated cases."""
    from litigation.matching import name_regex
    pats: list[re.Pattern] = []
    ids = _ids(case)
    nums = [case.get("case_number") or "", ids.get("connected") or ""]
    if ids.get("case_number") and ids.get("case_year"):
        nums.append(f"{ids['case_number']}/{ids['case_year']}")
    if ids.get("diary_number") and ids.get("diary_year"):
        nums.append(f"{ids['diary_number']}/{ids['diary_year']}")
    seen = set()
    for num in nums:
        # "SLP(C) 3713/2023", "C.A. No. 841-842/2023", "3713 of 2023" → number + year in any style
        for n, y in re.findall(r"(\d{2,6})(?:-\d{2,6})?\s*(?:/|of|-)\s*((?:19|20)\d{2})", str(num), re.I):
            if (n, y) not in seen:
                seen.add((n, y))
                pats.append(re.compile(rf"\b{n}(?:-\d{{2,6}})?\s*(?:/|of|-)\s*{y}\b", re.I))
    names = ([ids.get("registered_name"), case.get("company_name")]
             + list(_search(case).get("listed_as") or []) + list(case.get("listed_as") or []))
    seen_rx = set()
    for name in names:
        rx = name_regex(name or "")
        if rx is not None and rx.pattern not in seen_rx:
            seen_rx.add(rx.pattern)
            pats.append(rx)
    sym = case.get("symbol") or ""
    if sym and not pats:
        pats.append(re.compile(rf"\b{re.escape(sym)}\b", re.I))
    return pats


_HDR = re.compile(r"(DAILY CAUSE LIST FOR DATED[^\n]*|COURT NO\.?\s*:?\s*\d+[^\n]*|CHIEF JUSTICE'S COURT|"
                  r"\[?\s*JUDG(?:E)?MENT BY[^\]\n]*\]?|REGULAR HEARING|MISCELLANEOUS HEARING|FRESH MATTERS|"
                  r"AFTER NOTICE|FOR JUDG(?:E)?MENTS?[^\n]*|PRONOUNCEMENT[^\n]*|\(TIME\s*:[^)]*\))", re.I)


def _section_context(text: str, pos: int, lookback: int = 9000) -> str:
    """Court / section headings above a hit. Cause lists put 'COURT NO.' and the
    verdict marker ('JUDGMENT BY :- HON'BLE ...' / 'FOR JUDGEMENT', with or without
    brackets) above the items. A hit inside a verdict section is labelled so."""
    from litigation.matching import is_verdict
    seg = text[max(0, pos - lookback):pos]
    court = list(re.finditer(r"COURT NO\.?\s*:?\s*\d+|CHIEF JUSTICE'S COURT", seg, re.I))
    if court:
        seg = seg[court[-1].start():]
    out, seen = [], set()
    for m in _HDR.finditer(seg):
        h = " ".join(m.group(0).split())
        if h.upper() not in seen:
            seen.add(h.upper())
            out.append(h)
    ctx = " | ".join(out[-6:])
    try:
        sc = "SUPREME COURT" in text[:3000].upper()
        if is_verdict(text.upper(), pos, sc=sc):
            ctx = "VERDICT SECTION — listed for pronouncement of judgment | " + ctx
    except Exception:
        pass
    return ctx


def find_snippets(text: str, case: dict, window: int = 1200, cap: int = 20) -> list[str]:
    hits: list[tuple[int, int]] = []
    for p in _terms(case):
        for m in p.finditer(text):
            hits.append((max(0, m.start() - window), min(len(text), m.end() + window)))
            if len(hits) >= cap * 3:
                break
    if not hits:
        return []
    hits.sort()
    merged = [list(hits[0])]
    for a, b in hits[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = []
    for a, b in merged[:cap]:
        ctx = _section_context(text, min(b, a + window))
        out.append((f"[Section above this entry: {ctx}]\n" if ctx else "") + text[a:b])
    return out


# ── Analysis ──────────────────────────────────────────────────────────────────

_PROMPT = """\
Today is {today}. The user downloaded this document from an Indian court / tribunal /
regulator website (likely: {portal}) to check on a legal case affecting a listed company.

TRACKED CASE
{case}

DOCUMENT: {filename}
{body}

Read it and answer ONLY with JSON:
{{
  "doc_type": "cause_list | case_status | daily_order | judgment | notice | other",
  "doc_date": "YYYY-MM-DD the document is for/dated, or null",
  "case_found": true/false  (does this document actually concern THIS case?),
  "found_as": "how it appears (case no. / parties) or null",
  "listing_note": "court no., item no., bench, time — if listed, else null",
  "stage": "{stages} | null if the document doesn't tell",
  "stage_date": "YYYY-MM-DD or null",
  "next_date": "YYYY-MM-DD next listing / pronouncement date or null",
  "outcome": "if decided: 'adverse: …' | 'favourable: …' | 'mixed: …' (from the company's shareholders' view) else null",
  "key_points": ["the 2-5 facts that matter for the share price"],
  "summary": "max 60 words, plain English",
  "action": "what the user should do next and when (e.g. 'listed for pronouncement tomorrow 10:30, Court 3, item 1 — position before the open')"
}}
If the case is not in the document, say so (case_found=false) and still give doc_type/doc_date.
How to read Indian cause lists: a line '[ JUDGMENT BY :- HON'BLE ... ]' (or a 'FOR JUDGMENT' /
'PRONOUNCEMENT' heading) above an entry means the VERDICT is delivered on the list date →
stage = listed_for_pronouncement, next_date = the list date. Under 'REGULAR HEARING' /
'MISCELLANEOUS HEARING' / 'FRESH' it is an ordinary hearing → stage = hearing, next_date = list date.
Only set case_found=true when the case number, or BOTH parties, clearly match the tracked case.
"""


def _portal_hint(case: dict) -> str:
    from litigation.portals import official_links
    off = [l["label"] for l in official_links(case) if l["kind"] == "official"]
    return ", ".join(off[:3]) or "court website"


def _case_blob(c: dict) -> str:
    keep = ("symbol", "company_name", "title", "court", "case_number", "counterparty", "company_role",
            "subject", "stage", "stage_date", "next_date", "listing_note", "outcome")
    return json.dumps({k: c.get(k) for k in keep if c.get(k)}, ensure_ascii=False, indent=1)


def _analyze(case: dict, path: Path, text: str, snippets: list[str] | None) -> dict:
    ext = path.suffix.lower()
    files = None
    if snippets:
        body = "Relevant passages (the document is long; these are every place the case is mentioned):\n\n" + \
               "\n\n---\n\n".join(snippets)
    elif text and len(text) <= _INLINE_LIMIT:
        body = "Full text:\n" + text
    else:
        # image, scanned PDF, or a long doc with no textual hit → let Claude open the file
        body = ("(Open the attached file and read it. " +
                ("It is long — search it for the case number / party names. " if len(text) > _INLINE_LIMIT else "") +
                ")" + (f"\nExtracted text (may be partial):\n{text[:8000]}" if text else ""))
        files = [str(path)]
    prompt = _PROMPT.format(today=datetime.now().date().isoformat(), portal=_portal_hint(case),
                            case=_case_blob(case), filename=path.name, body=body,
                            stages=" | ".join(STAGES))
    try:
        out = generate(prompt, timeout=600, think=True, files=files, max_turns=15 if files else None)
    except Exception as exc:
        return {"error": str(exc), "case_found": False, "summary": f"Analysis failed: {exc}"}
    d = extract_json(out)
    if not isinstance(d, dict):
        return {"case_found": False, "summary": (out or "")[:400], "doc_type": "other"}
    if ext in _IMAGE:
        d.setdefault("doc_type", "case_status")
    return d


def _apply(case: dict, res: dict, path: Path, source: str) -> dict:
    """Push the analysis into the case (if found), log + reassess, close checks."""
    from litigation import analyzer as an
    from litigation import checks
    from litigation.tracker import _ingest_case
    changed = {}
    if res.get("case_found"):
        upd = {k: res.get(k) for k in ("stage", "stage_date", "next_date", "outcome", "listing_note")
               if res.get(k) not in (None, "", "null", "unknown")}
        upd = an._norm_case({**case, **upd})
        upd["case_key"] = case["case_key"]
        upd["date_source"] = f"attached: {path.name}"
        upd["update"] = res.get("summary") or ""
        changed = {k: upd.get(k) for k in ("stage", "next_date", "outcome")
                   if upd.get(k) != case.get(k)}
        _ingest_case(case["symbol"], upd, f"document {path.name}")
        if not changed:                       # nothing new, still record the date source
            ldb.execute("UPDATE lit_cases SET listing_note=COALESCE(?, listing_note), date_source=?, "
                        "last_checked=? WHERE id=?",
                        (upd.get("listing_note"), upd["date_source"],
                         datetime.now().isoformat(timespec="seconds"), case["id"]))
        checks.complete_for_case(case["id"])
    auto = source.startswith("auto:")
    if not (auto and not res.get("case_found")):       # auto-scan name-only hits stay silent
        ldb.add_event(case["id"], case["symbol"], "document", None, res.get("doc_type"),
                      f"{path.name}: {res.get('summary') or ''}")
        ldb.add_attachment(case["id"], path.name, str(path), source, {**res, "changed": changed})
    return {"case_id": case["id"], "symbol": case["symbol"], "title": case.get("title"),
            "changed": changed, **res}


def _case(case_id: int) -> dict:
    rows = ldb.query("SELECT * FROM lit_cases WHERE id=?", (case_id,))
    if not rows:
        raise ValueError(f"case {case_id} not found")
    c = rows[0]
    if not c.get("company_name"):
        from litigation.sources import company_info
        c["company_name"] = company_info(c["symbol"]).get("name")
    return c


def analyze_for_case(case_id: int, path: Path, source: str = "upload") -> dict:
    case = _case(case_id)
    text = extract_text(path)
    snippets = find_snippets(text, case) if len(text) > _INLINE_LIMIT else None
    log.info(f"attach[{case['symbol']}#{case_id}]: {path.name} text={len(text)} "
             f"snippets={len(snippets) if snippets is not None else '-'}")
    return _apply(case, _analyze(case, path, text, snippets), path, source)


def scan_document(path: Path, source: str = "upload") -> dict:
    """Check one document (e.g. tomorrow's cause list) against every open tracked
    case. Only cases whose case no. / parties appear are sent to Claude."""
    from litigation.analyzer import tier
    text = extract_text(path)
    cases = [c for c in ldb.query("SELECT * FROM lit_cases WHERE status IN ('active','immaterial') "
                                  "AND stage NOT IN ('decided','disposed')")
             if tier(c)[0] in ("material", "near") or c.get("stage") in ldb.HOT_STAGES or c.get("next_date")]
    if not text:
        return {"file": path.name, "scanned": len(cases), "matches": [],
                "note": "No extractable text (scanned PDF / image) — attach it to a specific case instead, "
                        "so Claude can read it visually against that case."}
    from litigation.sources import company_info
    hits = []
    for c in cases:
        c["company_name"] = c.get("company_name") or company_info(c["symbol"]).get("name")
        sn = find_snippets(text, c, window=900, cap=8)
        if sn:
            hits.append((c, sn))
    log.info(f"scan: {path.name} — {len(text)} chars, {len(cases)} cases checked, {len(hits)} candidate(s)")
    results = [_apply(c, _analyze(c, path, text, sn), path, source) for c, sn in hits]
    if not results:
        ldb.add_attachment(None, path.name, str(path), source,
                           {"doc_type": "cause_list", "case_found": False,
                            "summary": f"None of {len(cases)} tracked cases appear in this document."})
    return {"file": path.name, "scanned": len(cases), "matches": results,
            "note": None if results else f"None of the {len(cases)} tracked open cases appear in this document."}


def _scan_warnings(lists: list[dict]) -> list[str]:
    """Silent failure is the worst case — say so when the Supreme Court's lists for
    the next working day couldn't be fetched (site down / rate-limiting us)."""
    from datetime import date, timedelta
    if date.today().weekday() >= 5:                # weekend: Monday's list may not be out yet
        return []
    nxt = date.today() + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    sc = [x for x in lists if x.get("court") == "Supreme Court" and x["date"] in (date.today().isoformat(), nxt.isoformat())]
    if not sc:
        return [f"Supreme Court lists for {nxt:%a %d %b} couldn't be downloaded (court holiday, site down or rate-limiting us) — "
                "check sci.gov.in/cause-list yourself, or wait for the next scan."]
    return []


def auto_scan_public_lists() -> int:
    """Download the free, captcha-free cause lists (Supreme Court daily + advance,
    Delhi HC, NCLAT) and scan each once against every tracked case. A case found
    under a verdict heading becomes 'verdict date fixed' → Act-now alert."""
    from urllib.parse import urlparse
    from litigation.portals import auto_cause_lists
    from litigation.sources import _get
    done = {r["source"] for r in ldb.query("SELECT DISTINCT source FROM lit_attachments WHERE source LIKE 'auto:%'")}
    lists = auto_cause_lists()
    n = scanned = verdict_items = 0
    for cl in lists:
        src = f"auto: {cl['label']} | {cl['url']}"
        if src in done:
            continue
        host = urlparse(cl["url"])
        r = _get(cl["url"], {"Referer": f"{host.scheme}://{host.netloc.replace('api.', 'www.')}/"},
                 tries=2, timeout=120)
        if r is None or r.content[:4] != b"%PDF":
            continue
        p = save_upload(Path(cl["url"]).name, r.content, None)
        res = scan_document(p, source=src)
        found = [m for m in res["matches"] if m.get("case_found")]
        n += len(found)
        scanned += 1
        # Discovery-independent: every listed company in this list's verdict section
        # (runs after the tracked-case scan so an already-tracked case is reused).
        vnew = 0
        try:
            from litigation import verdicts
            vnew = verdicts.process(extract_text(p), cl.get("court") or "", cl["date"], cl["url"],
                                    Path(cl["url"]).name)
            verdict_items += vnew
        except Exception as exc:
            log.warning(f"auto-scan: verdict watch failed on {cl['label']} — {exc}")
        if not found and not vnew:
            p.unlink(missing_ok=True)              # keep disk tidy: nothing of ours in it
    log.info(f"auto-scan: {scanned} new list(s) of {len(lists)} available → {n} tracked case(s) found")
    try:                                           # summary for the dashboard header
        from collections import Counter
        from config import BASE_DIR as _B
        (_B / "db" / "cache" / "autoscan.json").write_text(json.dumps({
            "ran": datetime.now().isoformat(timespec="minutes"), "available": len(lists),
            "new_scanned": scanned, "found": n, "verdict_items": verdict_items,
            "warnings": _scan_warnings(lists),
            "courts": dict(Counter(x.get("court", "?") for x in lists)),
            "dates": sorted({x["date"] for x in lists})}), encoding="utf-8")
    except Exception as exc:
        log.debug(f"auto-scan summary not written: {exc}")
    return n
