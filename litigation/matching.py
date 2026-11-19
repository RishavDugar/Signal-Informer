"""
Reading court schedules (cause lists) reliably — shared by the live scanner and
the backtest.

  name_regex(name)     company name → regex that survives how courts print it
                       ('M/S NESTLE INDIA LTD.', 'BHUSHAN POWER AND STEEL LTD.')
  sections(text)       where each verdict / hearing section starts and ends
  is_verdict(...)      is this position inside a 'pronounce a verdict' section?
  verdict_hits(...)    every listed company named in a verdict section

Supreme Court lists mark verdicts in several ways — all seen in real lists:
    [ JUDGMENT BY :- HON'BLE MR. JUSTICE ... ]
    JUDGMENT BY :- HON'BLE MR. JUSTICE DIPANKAR DATTA        (no brackets)
    JUDGMENT BY : HON. THE CHIEF JUSTICE
    FOR JUDGEMENT                                             (sic — with an E)
and verdict items are numbered from 1501. The section lasts until the next
hearing heading or a different court; a page break repeats the court header,
which must NOT end the section.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from functools import lru_cache

# ── names ─────────────────────────────────────────────────────────────────────

_SUFFIX = {"limited", "ltd", "pvt", "private", "inc", "plc", "llp", "the"}
_CONNECT = {"and", "of", "the", "&"}
_JOIN = r"[\W_]+(?:(?:AND|OF|THE)[\W_]+)?"


def fold(s: str) -> str:
    """'Nestlé' → 'NESTLE'; any accent / odd unicode → plain ASCII, upper case."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().upper()


def name_regex(name: str) -> re.Pattern | None:
    """Regex for a company as a court would print it. Keeps the whole distinctive
    phrase ('RELIANCE INFRASTRUCTURE', not just 'RELIANCE'), tolerates 'AND' / 'OF'
    / '&' between words, drops trailing LTD / LIMITED / PVT. A lone short word
    (ITC, SIEMENS, ARVIND) must be followed by LTD / LIMITED — otherwise it
    collides with people's names. 'BANK OF INDIA' must not match inside
    'RESERVE BANK OF INDIA' / 'STATE BANK OF INDIA'."""
    words = re.findall(r"[A-Z0-9]+", fold(re.sub(r"(?i)\bm/s\.?", " ", name or "")))
    while words and words[-1].lower() in _SUFFIX:
        words.pop()
    while words and words[0].lower() == "the":
        words.pop(0)
    words = [w for w in words if w.lower() not in _CONNECT][:4]
    if not words:
        return None
    body = _JOIN.join(map(re.escape, words))
    if len(words) == 1:
        body += r"[\W_]+(?:LTD|LIMITED|PVT|PRIVATE)"
    guard = "".join(rf"(?<!{p}[\W_])" for p in _PREFIXES)
    return re.compile(rf"(?<![A-Z0-9]){guard}{body}(?![A-Z0-9])", re.I)


_PREFIXES = ("RESERVE", "STATE", "UNION", "CENTRAL", "NEW", "PUNJAB")


def search_text(text: str) -> str:
    """Text prepared for name_regex: accents folded, upper case. Positions refer to
    THIS string — use it for every later lookup."""
    return text.upper() if text.isascii() else fold(text)


# ── sections ──────────────────────────────────────────────────────────────────

_COURT = re.compile(r"COURT\s+NO\.?\s*:?\s*(\d+)|(CHIEF JUSTICE'?S COURT)|(REGISTRAR COURT[^\n]*)", re.I)
_VERDICT = re.compile(r"JUDG(?:E)?MENT\s+BY\b|^\s*FOR\s+JUDG(?:E)?MENTS?\s*$|FOR\s+PRONOUNCEMENT|"
                      r"PRONOUNCEMENT\s+OF\s+JUDG(?:E)?MENTS?|JUDG(?:E)?MENTS?\s+TO\s+BE\s+PRONOUNCED",
                      re.I | re.M)
# Section headings only (own line) — item text like 'FOR ADMISSION and I.R.' is not one.
_HEARING = re.compile(r"^\s*\[?\s*(MISCELLANEOUS HEARING|REGULAR HEARING|FINAL HEARING|"
                      r"FRESH\s*\(FOR ADMISSION\)|AFTER NOTICE|FRESHLY ADJOURNED)", re.I | re.M)


def sections(text: str) -> tuple[list[int], list[str]]:
    """Boundaries: positions and the mode that starts there ('verdict' / 'hearing'
    / 'none'). A new court resets to 'none'; the same court repeated on the next
    page (page header) keeps the current mode."""
    ev: list[tuple[int, str, str | None]] = []
    for m in _COURT.finditer(text):
        ev.append((m.start(), "court", (m.group(1) or m.group(2) or m.group(3) or "").strip().upper()))
    for m in _VERDICT.finditer(text):
        ev.append((m.start(), "verdict", None))
    for m in _HEARING.finditer(text):
        # the boiler-plate note above each bench ('...REGULAR HEARING MATTERS WILL BE
        # TAKEN UP') is not a heading
        line = text[m.start():text.find("\n", m.start()) if "\n" in text[m.start():] else len(text)]
        if "WILL BE TAKEN UP" in line.upper():
            continue
        ev.append((m.start(), "hearing", None))
    ev.sort()
    pos, modes = [0], ["none"]
    court, mode = None, "none"
    for p, kind, val in ev:
        if kind == "court":
            if val != court:
                court, mode = val, "none"
            else:
                continue
        else:
            mode = kind
        pos.append(p)
        modes.append(mode)
    return pos, modes


def mode_at(bounds: tuple[list[int], list[str]], p: int) -> str:
    pos, modes = bounds
    return modes[bisect.bisect_right(pos, p) - 1]


# An entry starts with its item number followed (same or next line) by the case
# type — 'SINGHANIA- 1594' (an advocate's enrolment no.) is not an item number.
_CASE_TYPE = (r"(?:C\.\s?A\.|CIVIL APPEAL|SLP|S\.L\.P|W\.\s?P|CRL|T\.\s?P\.|T\.\s?C\.|DIARY|CURATIVE|"
              r"R\.\s?P\.|CONMT|CONTEMPT|M\.\s?A\.|MA\b|O\.\s?S\.|ARBIT|REVIEW|TRANSFER|E\.\s?P\.|SMW|SMC|"
              r"REF|MISC|TP\b|WP\b|CA\b)")
# '1501 C.A. NO. ...', '119\nCRL.A. NO. ...', connected entries '119.\n1\nCONNECTED\nC.A. ...'
# and '1501\n.2\nCONNECTED\nC.A. ...' → group 1 = the item number.
_ITEM_RX = re.compile(rf"(?m)^[ \t]*(\d{{1,4}})(?:[ \t]*\.?[ \t]*\n?[ \t]*\.?[ \t]*\d{{1,3}}[ \t]*\n[ \t]*CONNECTED)?"
                      rf"[ \t]*\.?[ \t]*\n?[ \t]*{_CASE_TYPE}", re.I)


def _item_no(text: str, p: int) -> int | None:
    nums = _ITEM_RX.findall(text[max(0, p - 900):p])
    return int(nums[-1]) if nums else None


def is_verdict(text: str, p: int, bounds=None, sc: bool = True) -> bool:
    """Inside a verdict section. For the Supreme Court the entry must also carry a
    verdict item number (1501-1599) when one is visible — ordinary items listed
    later in the same court (no new heading) are hearings, not verdicts."""
    mode = mode_at(bounds or sections(text), p)
    if not sc:
        return mode == "verdict"
    n = _item_no(text, p)
    if n is not None:
        return 1501 <= n <= 1599
    return mode == "verdict"


_ITEM = _ITEM_RX


_ADV = re.compile(r"-\s*\d{2,4}\b|\[(?:P|R|CAVEAT|IMPL|INT)|^\s*\[|ADVOCATE|LAW OFFICES|& CO\b|ASSOCIATES|"
                  r"PARTNERS|CHAMBERS|SOLICITORS|^\s*(?:IA|I\.A\.)\s+NO|^\s*\{|^\s*\[PAGE", re.I)
_ROMAN = re.compile(r"^\s*[IVXL]+(?:-[A-Z])?\s*$")
_SKIP_LINE = re.compile(r"^\s*(?:\d+|CONNECTED|DAILY CAUSE LIST.*|COURT NO.*|SUPREME COURT OF INDIA|\.\d+|"
                        r"CHIEF JUSTICE'?S COURT|HON'?BLE.*|SNO\..*|PETITIONER.*|ADVOCATE|NOTE.*|\(TIME.*|"
                        r"\d{1,4}\s+(?:SLP|S\.L\.P|C\.\s?A|CIVIL|W\.\s?P|T\.\s?[PC]|CRL|DIARY|CURATIVE|R\.\s?P|"
                        r"CONMT|CONTEMPT|M\.\s?A|O\.\s?S|ARBIT|REVIEW|TRANSFER).*|"
                        r"(?:IN\s+)?(?:SLP|C\.\s?A\.|W\.\s?P|T\.\s?P|CRL|DIARY|CURATIVE|R\.\s?P).*NO\..*|"
                        r"NO\.\s*$|[\d\-/ ]+/\d{4}.*)$", re.I)


def _party(lines: list[str]) -> str:
    """First party name in a run of lines (joined if it wraps: '... ENGINEERS AND' + 'BUILDERS')."""
    out: list[str] = []
    for ln in lines:
        ln = ln.strip()
        if not ln or _ROMAN.match(ln) or _SKIP_LINE.match(ln):
            if out:
                break
            continue
        if _ADV.search(ln):
            if out:
                break
            continue
        out.append(ln)
        if not re.search(r"(?:\bAND|\bOF|&|,|\bTHE|\bFOR)\s*$", ln, re.I) or len(out) >= 3:
            break
    return re.sub(r"\s+\.\s*", " ", " ".join(out)).strip(" .,")


def item_at(text: str, p: int, width: int = 700) -> dict:
    """The schedule entry around position p: item no., case no., petitioner, respondent."""
    a = max(0, p - width)
    starts = [m for m in _ITEM.finditer(text, a, p)]
    s = starts[-1].start() if starts else max(0, p - 250)
    nxt = _ITEM.search(text, p + 1, min(len(text), p + width))
    e = nxt.start() if nxt else min(len(text), p + 400)
    chunk = text[s:e]
    m0 = _ITEM.match(chunk) if starts else None
    mm = m0 or re.search(r"\b(\d{1,4})\b", chunk)
    item = mm.group(1) if mm else None
    case_no = re.search(r"((?:SLP|C\.A\.|CIVIL APPEAL|W\.P\.|WP|T\.P\.|CURATIVE|R\.P\.|Crl\.A\.|"
                        r"CONMT\.PET\.|M\.A\.|DIARY)[^\n]{0,40}?No\.?\s*[\d\-/ \n]+(?:/\d{4})?)", chunk, re.I)
    parts = re.split(r"(?im)^\s*VERSUS\s*$", chunk, maxsplit=1)
    pet = _party(parts[0].splitlines()) if parts else ""
    res = _party(parts[1].splitlines()) if len(parts) > 1 else ""
    return {"item": item,
            "case_no": " ".join(case_no.group(1).split()) if case_no else None,
            "petitioner": pet or None, "respondent": res or None,
            "versus": f"{pet} vs {res}" if (pet or res) else " ".join(chunk.split())[:160]}


def court_at(text: str, p: int) -> str | None:
    m = None
    for m in _COURT.finditer(text, max(0, p - 60000), p):
        pass
    if not m:
        return None
    if m.group(1):
        return f"Court {m.group(1)}"
    return "Chief Justice's Court" if m.group(2) else " ".join((m.group(3) or "").split()).title()


_STAMP = re.compile(r"(\d{2})-(\d{2})-(\d{4})\s+(\d{2}):(\d{2}):\d{2}")


def published_at(text: str) -> str | None:
    """SC lists end each bench with 'NEW DELHI / 14-10-2026 17:40:52 / ADDITIONAL
    REGISTRAR'. The LATEST stamp is when the whole list was final."""
    iso = sorted(f"{y}-{mo}-{d}T{h}:{mi}" for d, mo, y, h, mi in _STAMP.findall(text))
    return iso[-1] if iso else None


def signed_after(text: str, p: int) -> str | None:
    """When the bench holding position p was signed — the first registrar stamp
    after it (each bench's section ends with its signature)."""
    m = _STAMP.search(text, p)
    if not m:
        return None
    d, mo, y, h, mi = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}"


# ── listed-company universe ───────────────────────────────────────────────────

# Names that collide with common words / government parties in schedules.
_SKIP_NAMES = {"INDIA", "UNION", "STATE", "BANK"}


@lru_cache(maxsize=1)
def listed_patterns(min_mcap_cr: float = 5000) -> tuple[tuple[str, str, re.Pattern], ...]:
    """(symbol, company name, regex) for every F&O stock plus any listed company
    with market cap ≥ min_mcap_cr. Longest names first so 'RELIANCE INFRASTRUCTURE'
    wins over a shorter overlapping name."""
    from litigation.sources import bse_scrips, fno_lots
    scrips = bse_scrips()
    try:
        fno = set(fno_lots())
    except Exception:
        fno = set()
    out = []
    for sym, v in scrips.items():
        if not (sym in fno or (v.get("mcap_cr") or 0) >= min_mcap_cr):
            continue
        name = v.get("name") or ""
        rx = name_regex(name)
        if rx is None or fold(name).strip() in _SKIP_NAMES:
            continue
        out.append((sym, name, rx))
    out.sort(key=lambda t: -len(t[2].pattern))
    return tuple(out)


def _verdict_regions(t: str, bounds, sc: bool) -> list[tuple[int, int]]:
    """Text spans that can hold verdict items: sections in verdict mode, plus (SC)
    every entry numbered 1501-1599. Matching only inside these keeps a 300-page
    list to a few KB of regex work."""
    pos, modes = bounds
    spans = [(pos[k], pos[k + 1] if k + 1 < len(pos) else len(t))
             for k in range(len(pos)) if modes[k] == "verdict"]
    if sc:
        items = list(_ITEM_RX.finditer(t))
        for i, m in enumerate(items):
            if 1501 <= int(m.group(1)) <= 1599:
                spans.append((m.start(), items[i + 1].start() if i + 1 < len(items) else min(len(t), m.start() + 4000)))
    spans.sort()
    merged: list[list[int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def verdict_hits(text: str, whole_doc_is_verdicts: bool = False,
                 patterns=None, sc: bool = True) -> list[dict]:
    """Every listed company named inside a verdict section of this schedule.
    whole_doc_is_verdicts: True for judgment-only lists (Delhi HC 'j_' list)."""
    t = search_text(text)
    bounds = None if whole_doc_is_verdicts else sections(t)
    regions = [(0, len(t))] if whole_doc_is_verdicts else _verdict_regions(t, bounds, sc)
    hits, seen = [], set()
    for sym, name, rx in (patterns or listed_patterns()):
        for a, b in regions:
            for m in rx.finditer(t, a, b):
                if not (whole_doc_is_verdicts or is_verdict(t, m.start(), bounds, sc)):
                    continue
                court = court_at(t, m.start()) or ""
                if court.upper().startswith("REGISTRAR"):
                    continue                                   # registrar's list: procedural only
                it = item_at(t, m.start())
                key = (sym, it.get("item"), it.get("case_no"))
                if key in seen:
                    continue
                seen.add(key)
                hits.append({"symbol": sym, "company": name, **it, "court_no": court or None,
                             "signed": signed_after(t, m.start()), "pos": m.start()})
    return hits
