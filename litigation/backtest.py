"""
3-year replay of the legal tracker — how many real court / tribunal / regulator
rulings on listed Indian companies would it have caught, and when?

Pipeline (each stage caches to db/cache/backtest/ so re-runs are cheap):

  1. events   Claude + web search lists rulings in 9 slices (3 years x 3 forum
              groups) — big movers, small movers AND no-movers. Merged + deduped.
  2. prices   yfinance: abnormal return vs NIFTY for the first session that could
              react (T0 — next session if the ruling came after the close), T+1, T+2.
  3. replay   what the tracker would have known BEFORE the ruling:
                • Supreme Court  → the actual SC cause-list PDFs for the ruling day
                  (still public on api.sci.gov.in) are downloaded and searched for
                  the company / case number — exactly what the 21:15 scan does.
                  Hit inside '[ JUDGMENT BY ...]' = verdict known the evening before.
                • other forums   → historical lists aren't public, so the news
                  evidence ('verdict reserved on', 'to pronounce tomorrow') is used
                  and labelled as such.
                • a second per-ruling research pass: when the dispute became public,
                  when the verdict was reserved, court-recorded party names.
  4. score    alert level reached before the ruling, impact class, and whether the
              move was still catchable after the ruling (1-2 day drift).

    python -m litigation.backtest            # full run (re-uses caches)
    python -m litigation.backtest --refresh  # re-ask Claude for the event list
"""

from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from utils.logger import get_logger

log = get_logger("litigation.backtest")

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "db" / "cache" / "backtest"
OUT_JSON = ROOT / "db" / "litigation_backtest.json"
OUT_MD = ROOT / "docs" / "litigation_backtest.md"

START, END = date(2023, 11, 1), date(2026, 11, 18)
WINDOWS = [(date(2023, 11, 1), date(2024, 10, 31)),
           (date(2024, 11, 1), date(2025, 10, 31)),
           (date(2025, 11, 1), date(2026, 11, 18))]
FORUMS = {
    "sc": "the Supreme Court of India",
    "hc": "High Courts (Delhi, Bombay, Madras, Karnataka, Gujarat, Calcutta, etc.) and "
          "arbitral tribunals / awards being enforced or set aside",
    "trib": "NCLT, NCLAT, SAT (Securities Appellate Tribunal), SEBI final/interim orders, "
            "CCI, TDSAT, APTEL, CESTAT, ITAT, GST/tax authorities' large demand orders, DRT, NGT",
}

IMPACT = 0.03          # |abnormal move| ≥ 3% within T0..T+2 counts as an impact
DRIFT = 0.02           # ≥ 2% further move after T0 close in the ruling's direction


# ── 1. events ────────────────────────────────────────────────────────────────

_EVENT_PROMPT = """\
You are compiling a research dataset of court, tribunal and regulator RULINGS / ORDERS
on companies LISTED ON THE NSE (India), pronounced between {a} and {b}.
Forum group for this list: {forum}.

Rules:
- Include rulings REGARDLESS of how the stock reacted — we need big movers, small
  movers and rulings where the stock did not move at all. Do not cherry-pick.
- Only rulings where a specific listed company was a party or was directly affected
  (sector-wide judgments are fine if you name the most affected listed companies,
  one row per company, max 3 companies per judgment).
- Aim for 20-30 distinct rulings. Do at least 8 web searches (LiveLaw, Bar & Bench,
  Moneycontrol, Economic Times, Business Standard, Mint, company BSE filings).
- ruling_date = the day the order/judgment was PRONOUNCED (not when it was reported).

Return ONLY a JSON array; each item:
{{"symbol": "NSE symbol, no .NS",
  "company": "full company name",
  "forum": "e.g. Supreme Court / Delhi High Court / NCLAT / SAT / SEBI / CCI",
  "case": "short description (≤ 15 words)",
  "case_number": "official number if known else null",
  "ruling_date": "YYYY-MM-DD",
  "ruling_time": "pre_market | market_hours | after_market | unknown",
  "direction": "adverse | favourable | mixed | neutral  (for the company)",
  "claim_cr": number or null  (company's own money at stake, ₹ crore),
  "case_known_since": "YYYY-MM-DD earliest date the dispute was publicly reported, or null",
  "reserved_on": "YYYY-MM-DD when judgment was reserved / arguments concluded, or null",
  "date_known_in_advance": true/false  (was the pronouncement date public ≥ 1 day before —
        e.g. listed in the cause list, 'SC to pronounce verdict tomorrow' reports),
  "advance_evidence": "one line: how the date / pendency was known, or null",
  "source_url": "best source"}}
"""


def _slice(key: str, a: date, b: date, refresh: bool) -> list[dict]:
    path = CACHE / f"events_{key}_{a:%Y%m}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    from news_analyzer.llm import extract_json, generate
    prompt = _EVENT_PROMPT.format(a=a.isoformat(), b=b.isoformat(), forum=FORUMS[key])
    for attempt in range(2):
        try:
            raw = generate(prompt, web=True, max_turns=40, timeout=900)
            rows = extract_json(raw)
            if isinstance(rows, list) and rows:
                for r in rows:
                    r["_slice"] = f"{key}:{a:%Y}"
                path.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
                log.info(f"backtest: slice {key} {a}..{b} → {len(rows)} events")
                return rows
            log.warning(f"backtest: slice {key} {a} returned no JSON (attempt {attempt + 1})")
        except Exception as exc:
            log.warning(f"backtest: slice {key} {a} failed — {exc}")
    return []


_ALIAS = {"VI": "IDEA", "VODAFONEIDEA": "IDEA", "ZEE": "ZEEL", "JPASSOCIAT": "JPASSOCIAT"}


def _norm_sym(s: str) -> str:
    s = re.sub(r"\.(NS|BO)$", "", (s or "").upper().strip()).replace(" ", "")
    return _ALIAS.get(s, s)


# Known rulings the web sweep may miss (it did miss this one) — always included.
SEEDS = [
    {"symbol": "YESBANK", "company": "Yes Bank Limited", "forum": "Supreme Court",
     "case": "AT1 bond write-off: SC dismisses appeals, upholds Bombay HC quashing the write-off",
     "ruling_date": "2026-10-15", "ruling_time": "market_hours", "direction": "adverse",
     "claim_cr": 8415, "case_known_since": "2020-03-14", "_slice": "seed"},
]


def gather_events(refresh: bool = False) -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    jobs = [(k, a, b) for a, b in WINDOWS for k in FORUMS]
    with ThreadPoolExecutor(3) as ex:
        parts = list(ex.map(lambda j: _slice(j[0], j[1], j[2], refresh), jobs))
    parts.append([dict(x) for x in SEEDS])
    out, seen = [], {}
    for rows in parts:
        for r in rows:
            sym, d = _norm_sym(r.get("symbol")), (r.get("ruling_date") or "")[:10]
            if not sym or not re.match(r"\d{4}-\d{2}-\d{2}$", d) or not (START.isoformat() <= d <= END.isoformat()):
                continue
            r["symbol"], r["ruling_date"] = sym, d
            # same company, rulings within 3 days → same event
            dup = next((o for o in seen.get(sym, [])
                        if abs((date.fromisoformat(o["ruling_date"]) - date.fromisoformat(d)).days) <= 3), None)
            if dup:
                for k, v in r.items():
                    if dup.get(k) in (None, "", "unknown") and v not in (None, ""):
                        dup[k] = v
                continue
            seen.setdefault(sym, []).append(r)
            out.append(r)
    out.sort(key=lambda r: r["ruling_date"])
    log.info(f"backtest: {sum(map(len, parts))} raw → {len(out)} unique events")
    return out


# ── 2. prices ────────────────────────────────────────────────────────────────

def _download(symbols: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    path = CACHE / "prices.pkl"
    have: dict[str, pd.DataFrame] = pd.read_pickle(path) if path.exists() else {}
    need = [s for s in set(symbols) | {"^NSEI"} if s not in have]
    for i in range(0, len(need), 40):
        chunk = need[i:i + 40]
        tick = [s if s.startswith("^") else f"{s}.NS" for s in chunk]
        df = yf.download(tick, start="2023-09-01", end="2026-11-20", progress=False,
                         auto_adjust=True, group_by="ticker", threads=True)
        for s, t in zip(chunk, tick):
            try:
                sub = df[t].dropna(how="all") if isinstance(df.columns, pd.MultiIndex) else df
                have[s] = sub[["Open", "Close"]].dropna() if len(sub) else pd.DataFrame()
            except KeyError:
                have[s] = pd.DataFrame()
    pd.to_pickle(have, path)
    return have


def reaction(px: pd.DataFrame, nifty: pd.DataFrame, d: str, when: str) -> dict | None:
    """Abnormal returns (stock − NIFTY) for T0 (first session able to react), T+1,
    T+2, plus the stock's own normal daily abnormal volatility (60 sessions before)
    so 'impact' means 'unusual for THIS stock', not just ≥ 3%."""
    if px is None or px.empty:
        return None
    idx = px.index
    i = idx.searchsorted(pd.Timestamp(d))
    if i >= len(idx) or i < 21:
        return None
    if when == "after_market" and idx[i].date().isoformat() == d:
        i += 1                                  # ruling after the close → next session reacts
    if i + 2 >= len(idx):
        return None
    c = px["Close"]
    n = nifty["Close"].reindex(idx).ffill()

    def ab(j0, j1):
        return float(c.iloc[j1] / c.iloc[j0] - 1 - (n.iloc[j1] / n.iloc[j0] - 1))

    hist = (c.pct_change() - n.pct_change()).iloc[max(1, i - 61):i - 1].dropna()
    sigma = float(hist.std()) if len(hist) > 15 else 0.02
    return {"t0": idx[i].date().isoformat(), "r0": ab(i - 1, i), "r1": ab(i, i + 1), "r2": ab(i + 1, i + 2),
            "cum": ab(i - 1, i + 2), "post": ab(i, i + 2), "sigma": sigma,
            "gap": float(px["Open"].iloc[i] / c.iloc[i - 1] - 1), "px_prev": float(c.iloc[i - 1])}


# ── 3. replay: Supreme Court cause lists ─────────────────────────────────────

_SC_CODES = ["M_J_1", "M_J_2", "M_J_3", "F_J_1", "F_J_2", "M_C_1", "M_C_2", "M_S_1", "M_R_1", "M_CC_1"]


_DAY_LOCKS: dict[str, "threading.Lock"] = {}
_LOCKS_GUARD = __import__("threading").Lock()


def _sc_texts(d: str) -> list[tuple[str, str]]:
    """[(url, text)] of every SC list for day d (daily lists + the advance list)."""
    import threading
    with _LOCKS_GUARD:
        lock = _DAY_LOCKS.setdefault(d, threading.Lock())
    with lock:
        return _sc_texts_locked(d)


def _sc_texts_locked(d: str) -> list[tuple[str, str]]:
    from litigation.attachments import extract_text
    from litigation.sources import _get
    folder = CACHE / "sc" / d
    if (folder / "_done").exists():
        return [(p.read_text(encoding="utf-8").split("\n", 1)[0], p.read_text(encoding="utf-8").split("\n", 1)[1])
                for p in sorted(folder.glob("*.txt"))]
    folder.mkdir(parents=True, exist_ok=True)
    urls = [f"https://api.sci.gov.in/jonew/cl/{d}/{c}.pdf" for c in _SC_CODES] + \
           [f"https://api.sci.gov.in/jonew/cl/advance/{d}/M_J.pdf"]
    out = []
    for u in urls:
        r = _get(u, {"Referer": "https://www.sci.gov.in/"}, tries=2, timeout=60)
        if r is None or len(r.content) < 1000:
            continue
        tmp = folder / (u.rsplit("/", 1)[1] + ".tmp.pdf")
        tmp.write_bytes(r.content)
        try:
            txt = extract_text(tmp)
        except Exception:
            txt = ""
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        if txt:
            name = u.split("/cl/")[1].replace("/", "_")
            (folder / f"{name}.txt").write_text(u + "\n" + txt, encoding="utf-8")
            out.append((u, txt))
        time.sleep(0.3)
    (folder / "_done").write_text("")
    return out


def _prev_workdays(d: date, n: int) -> list[date]:
    out, x = [], d
    while len(out) < n:
        x -= timedelta(days=1)
        if x.weekday() < 5:
            out.append(x)
    return out


def sc_replay(ev: dict) -> dict:
    """Search the ruling day's SC lists — what the 21:15 scan reads the evening
    before — for the case number, the company, and the party names as the court
    records them (e.g. a subsidiary). A name that hits more than 4 items that day
    (SBI, Union of India-type litigants) is too generic to count on its own."""
    from litigation.attachments import _section_context, _terms
    d = date.fromisoformat(ev["ruling_date"])
    case = {"case_number": ev.get("case_number") or "", "company_name": ev.get("company") or "",
            "symbol": ev["symbol"]}
    num_pats = [p for p in _terms(case) if re.search("[0-9]", p.pattern)]
    names = [ev.get("company") or ""] + list(ev.get("court_party_names") or [])
    name_pats = []
    for nm in names:
        pats = _terms({"company_name": nm})
        name_pats += [p for p in pats if not re.search("[0-9]", p.pattern)]
    res = {"lists_found": 0, "listed": False, "judgment_section": False, "match": None,
           "name_hits": 0, "where": None, "url": None}
    texts = _sc_texts(d.isoformat())
    res["lists_found"] = len(texts)

    def take(u, txt, m, how):
        ctx = _section_context(txt, m.start())
        jud = "JUDGMENT" in ctx.upper() or "PRONOUNCEMENT" in ctx.upper()
        if not res["listed"] or (jud and not res["judgment_section"]):
            res.update(listed=True, url=u, where=ctx[:200], match=how, judgment_section=jud)

    for u, txt in texts:
        for p in num_pats:
            for m in p.finditer(txt):
                take(u, txt, m, "case number")
    if not res["listed"] or not res["judgment_section"]:
        daily = [(u, txt) for u, txt in texts if "/advance/" not in u]   # advance repeats daily items
        for p in name_pats:                            # each name judged on its own specificity
            hits = [(u, txt, m) for u, txt in daily for m in p.finditer(txt)]
            res["name_hits"] = max(res["name_hits"], len(hits))
            if hits and len(hits) <= 4:
                for u, txt, m in hits:
                    take(u, txt, m, "party name")
            elif hits:                                 # generic name: credit only a verdict-section hit
                for u, txt, m in hits:
                    if "JUDGMENT" in _section_context(txt, m.start()).upper():
                        take(u, txt, m, "party name (verdict list)")
                        break
    return res


# ── 3b. replay: second, focused research pass per ruling ─────────────────────

_ENRICH_PROMPT = """\
For each Indian court / tribunal / regulator ruling below, establish what was PUBLICLY
KNOWN BEFORE the ruling day. Use web search (LiveLaw, Bar & Bench, Moneycontrol, ET,
Business Standard, company BSE filings). Be factual; null when you can't find it.

{items}

Return ONLY a JSON array, one object per ruling, same order:
{{"id": "<id as given>",
  "case_known_since": "YYYY-MM-DD — earliest public report that this dispute was in a court /
       tribunal / before the regulator (news or company filing), or null",
  "reserved_on": "YYYY-MM-DD — judgment reserved / arguments concluded, or null",
  "last_hearing_before": "YYYY-MM-DD — last hearing date before the ruling, or null",
  "date_known_in_advance": true/false — was the ruling/pronouncement DATE public at least the
       evening before (cause list, 'to pronounce verdict tomorrow', scheduled hearing reported)?,
  "advance_evidence": "one line with the source, or null",
  "court_party_names": ["party names exactly as the court lists them, incl. subsidiaries — e.g.
       'Delhi Airport Metro Express Pvt Ltd' for Reliance Infra; max 3"],
  "case_number": "official case number, or null",
  "ruling_time": "pre_market | market_hours | after_market | unknown"}}
"""


_GAP_NOTE = """
IMPORTANT: an earlier pass left case_known_since empty for these. Almost every dispute
that reaches a court, tribunal or regulator ruling was reported earlier — the original
order being appealed, the petition filing, an earlier hearing, an interim order, or the
company's own disclosure / annual-report contingent liability. Search for those (at
least 2 searches per ruling) and give the earliest such date. If only the month or year
is known, use YYYY-MM-01 / YYYY-01-01. Use null ONLY for genuinely sudden actions with
no earlier proceeding (e.g. a surprise regulator raid or ex-parte interim order).
"""


def _eid(e: dict) -> str:
    return f"{e['symbol']}@{e['ruling_date']}"


def enrich(events: list[dict], refresh: bool = False) -> None:
    from news_analyzer.llm import extract_json, generate
    path = CACHE / "enrich.json"
    done: dict = json.loads(path.read_text(encoding="utf-8")) if path.exists() and not refresh else {}
    todo = [e for e in events if _eid(e) not in done]
    if done:     # a re-run only fills what's missing → small, thorough batches
        batches = [(todo[i:i + 4], True) for i in range(0, len(todo), 4)]
    else:
        batches = [(b, False) for b in (todo[i:i + 10] for i in range(0, len(todo), 10))]
    # gap pass: rulings still lacking 'public since' -> small batches, approximate dates OK
    gaps = [e for e in events if _eid(e) in done and not done[_eid(e)].get("case_known_since")
            and not done[_eid(e)].get("_gap_pass")]
    batches += [(gaps[i:i + 4], True) for i in range(0, len(gaps), 4)]

    def one(job):
        batch, gap = job
        items = "\n".join(f"- id={_eid(e)} | {e.get('company')} ({e['symbol']}) | {e.get('forum')} | "
                          f"ruled {e['ruling_date']} | {e.get('case')}" for e in batch)
        for _ in range(2):
            try:
                prompt = _ENRICH_PROMPT.format(items=items) + (_GAP_NOTE if gap else "")
                rows = extract_json(generate(prompt, web=True, max_turns=40, timeout=900))
                if isinstance(rows, list) and rows:
                    for r in rows:
                        if isinstance(r, dict):
                            r["_gap_pass"] = gap
                    return rows
            except Exception as exc:
                log.warning(f"backtest: enrich batch failed — {exc}")
        return []

    with ThreadPoolExecutor(3) as ex:
        for rows in ex.map(one, batches):
            for r in rows:
                if isinstance(r, dict) and r.get("id"):
                    prev = done.get(r["id"], {})
                    done[r["id"]] = {**prev, **{k: v for k, v in r.items() if v not in (None, "", [])},
                                     "_gap_pass": r.get("_gap_pass") or prev.get("_gap_pass", False)}
            path.write_text(json.dumps(done, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info(f"backtest: enrichment covers {sum(1 for e in events if _eid(e) in done)}/{len(events)} rulings")

    for e in events:
        r = done.get(_eid(e))
        if not r:
            continue
        e["enriched"] = True
        for k in ("case_known_since", "reserved_on", "case_number", "last_hearing_before"):
            v = r.get(k)
            if v and (not e.get(k) or (k in ("case_known_since",) and str(v) < str(e.get(k)))):
                e[k] = v
        if r.get("date_known_in_advance"):
            e["date_known_in_advance"] = True
            e["advance_evidence"] = r.get("advance_evidence") or e.get("advance_evidence")
        if r.get("court_party_names"):
            e["court_party_names"] = [x for x in r["court_party_names"] if isinstance(x, str)][:3]
        if (e.get("ruling_time") in (None, "", "unknown")) and r.get("ruling_time"):
            e["ruling_time"] = r["ruling_time"]


# ── 4. score ─────────────────────────────────────────────────────────────────

LEVELS = {
    3: "Exact date known (alert the evening before)",
    2: "'Verdict any day' watch",
    1: "Case on radar, no date",
    0: "Surprise",
}
SHORT = {3: "Date known", 2: "Any-day watch", 1: "On radar", 0: "Surprise"}


def _iso(v) -> str | None:
    return v[:10] if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v) else None


def score(ev: dict) -> dict:
    d = date.fromisoformat(ev["ruling_date"])
    week_before = (d - timedelta(days=7)).isoformat()
    known = _iso(ev.get("case_known_since"))
    known_ok = bool(known and known <= week_before)
    reserved = _iso(ev.get("reserved_on"))
    reserved_ok = bool(reserved and reserved < ev["ruling_date"])
    last_hearing = _iso(ev.get("last_hearing_before"))
    sc = ev.get("sc") or {}
    is_sc = "supreme" in (ev.get("forum") or "").lower()

    if is_sc and sc.get("listed"):
        level = 3
        basis = ("SC schedule: in the verdict list" if sc.get("judgment_section")
                 else "SC schedule: listed that day") + f" ({sc.get('match')})"
    elif ev.get("date_known_in_advance") and (known_ok or reserved_ok or last_hearing):
        level, basis = 3, "news: date public in advance" + (" (SC list didn't match)" if is_sc else "")
    elif reserved_ok:
        level, basis = 2, f"verdict reserved {reserved}"
    elif known_ok:
        level, basis = 1, f"dispute public since {known}"
    else:
        level, basis = 0, "no public trace ≥ 7 days earlier"

    rx = ev.get("rx")
    out = {"level": level, "basis": basis, "is_sc": is_sc, "impact": None, "timing": None,
           "catch_after": False, "as_expected": None, "mcap_cr": None, "claim_pct": None}
    if not rx:
        return out
    day_thr = max(IMPACT, 2.5 * rx["sigma"])
    cum_thr = max(IMPACT, 2.5 * rx["sigma"] * 3 ** 0.5)
    if abs(rx["r0"]) >= day_thr:
        timing, mv = "same day", rx["r0"]
    elif abs(rx["cum"]) >= cum_thr and abs(rx["cum"] - rx["r0"]) >= abs(rx["cum"]) / 2:
        timing, mv = "1-2 days later", rx["cum"]
    else:
        timing, mv = "none", None
    out["timing"] = timing
    out["impact"] = "none" if mv is None else ("down" if mv < 0 else "up")
    out["thr"] = day_thr
    want = {"adverse": -1, "favourable": 1}.get((ev.get("direction") or "").lower())
    if mv is not None and want:
        out["as_expected"] = (mv < 0) == (want < 0)
    # catchable after the news: a further ≥ 2% from T0 close to T+2 close, in the
    # direction the ruling implies (or T0's direction for mixed / neutral rulings)
    sgn = want or (1 if rx["r0"] >= 0 else -1)
    out["catch_after"] = sgn * rx["post"] >= DRIFT
    bse = ev.get("bse") or {}
    if bse.get("mcap_now") and ev.get("_px_now"):
        out["mcap_cr"] = bse["mcap_now"] * rx["px_prev"] / ev["_px_now"]
        try:
            out["claim_pct"] = float(ev["claim_cr"]) / out["mcap_cr"] if ev.get("claim_cr") else None
        except (TypeError, ValueError):
            pass
    return out


# ── run ──────────────────────────────────────────────────────────────────────

def run(refresh: bool = False) -> dict:
    import config
    from litigation.sources import company_info, fno_lots
    # Facts must come from Claude's web search — never let a quota hit fall back to
    # the local model (no web access → it would invent dates).
    config.LLM_FALLBACK = False
    events = gather_events(refresh)
    enrich(events, refresh)
    prices = _download([e["symbol"] for e in events])
    nifty = prices.get("^NSEI")
    try:
        fno = set(fno_lots())
    except Exception:
        fno = set()

    sc_events = [e for e in events if "supreme" in (e.get("forum") or "").lower()]
    log.info(f"backtest: replaying {len(sc_events)} Supreme Court rulings against the real cause lists")
    with ThreadPoolExecutor(4) as ex:
        for e, r in zip(sc_events, ex.map(sc_replay, sc_events)):
            e["sc"] = r

    for e in events:
        px = prices.get(e["symbol"])
        e["bse"] = {"mcap_now": company_info(e["symbol"]).get("mcap_cr")}
        e["rx"] = reaction(px, nifty, e["ruling_date"], e.get("ruling_time") or "unknown")
        e["_px_now"] = float(px["Close"].iloc[-1]) if px is not None and not px.empty else None
        e["fno"] = e["symbol"] in fno
        e["score"] = score(e)

    summary = summarise(events)
    OUT_JSON.write_text(json.dumps({"generated": datetime.now().isoformat(timespec="minutes"),
                                    "summary": summary, "events": events},
                                   indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    OUT_MD.write_text(markdown(events, summary), encoding="utf-8")
    log.info(f"backtest: wrote {OUT_JSON.name} and {OUT_MD.name}")
    return summary


def _bucket(sc_: dict) -> str:
    """ahead (alert before) · after (catchable on the follow-through) · missed."""
    if sc_["level"] >= 2:
        return "ahead"
    if sc_["catch_after"]:
        return "after"
    return "missed"


def summarise(events: list[dict]) -> dict:
    ok = [e for e in events if e.get("rx")]
    s = {"events": len(events), "priced": len(ok), "fno": sum(1 for e in ok if e.get("fno")),
         "by_level": {}, "matrix": {}, "buckets": {}}
    for e in ok:
        sc_ = e["score"]
        s["by_level"][sc_["level"]] = s["by_level"].get(sc_["level"], 0) + 1
        s["matrix"].setdefault(sc_["timing"], {}).setdefault(sc_["level"], 0)
        s["matrix"][sc_["timing"]][sc_["level"]] += 1
    moved = [e for e in ok if e["score"]["timing"] != "none"]
    s["moved"] = len(moved)
    s["moved_same_day"] = sum(1 for e in moved if e["score"]["timing"] == "same day")
    s["moved_later"] = sum(1 for e in moved if e["score"]["timing"] == "1-2 days later")
    s["no_move"] = len(ok) - len(moved)
    for e in moved:
        b = _bucket(e["score"])
        s["buckets"][b] = s["buckets"].get(b, 0) + 1
    s["captured"] = s["buckets"].get("ahead", 0) + s["buckets"].get("after", 0)
    s["as_expected"] = sum(1 for e in moved if e["score"]["as_expected"] is True)
    s["against"] = sum(1 for e in moved if e["score"]["as_expected"] is False)
    s["dated_ahead"] = sum(1 for e in moved if e["score"]["level"] == 3)
    s["no_move_alerted"] = sum(1 for e in ok if e["score"]["timing"] == "none" and e["score"]["level"] >= 2)
    scs = [e for e in ok if e["score"]["is_sc"]]
    s["sc"] = {"rulings": len(scs),
               "lists": sum(1 for e in scs if (e.get("sc") or {}).get("lists_found")),
               "listed": sum(1 for e in scs if (e.get("sc") or {}).get("listed")),
               "verdict_list": sum(1 for e in scs if (e.get("sc") or {}).get("judgment_section"))}
    other = [e for e in ok if not e["score"]["is_sc"]]
    s["other_dated"] = sum(1 for e in other if e["score"]["level"] == 3)
    s["other"] = len(other)
    return s


def _p(x):
    return "—" if x is None else f"{x * 100:+.1f}%"


def markdown(events: list[dict], s: dict) -> str:
    b = s["buckets"]
    L = ["# Legal tracker — 3-year backtest", "",
         f"_Generated {datetime.now():%d %b %Y %H:%M}. Rulings {START:%b %Y} → {END:%b %Y}._", "",
         "## Headline", "",
         f"- **{s['priced']}** rulings on listed companies with usable prices ({s['events']} found, "
         f"{s['fno']} in today's F&O list).",
         f"- **{s['moved']}** moved the stock unusually (≥ 3% and ≥ 2.5× its normal daily move, vs NIFTY): "
         f"{s['moved_same_day']} on the ruling day, {s['moved_later']} only over the next 1-2 sessions. "
         f"**{s['no_move']}** didn't move.",
         f"- Of the {s['moved']} movers: **{b.get('ahead', 0)}** were on an alert *before* the ruling "
         f"({s['dated_ahead']} with the exact date), **{b.get('after', 0)}** more were still catchable "
         f"after it (a further ≥ 2% in the ruling's direction by T+2), **{b.get('missed', 0)}** missed. "
         f"→ **{s['captured']}/{s['moved']} catchable**.",
         f"- Direction: {s['as_expected']} movers went the way the ruling implied, {s['against']} the "
         f"opposite way (market had priced it / 'sell the news').",
         f"- Supreme Court: {s['sc']['rulings']} rulings, schedules downloaded for {s['sc']['lists']}, case "
         f"found on the schedule for **{s['sc']['listed']}** ({s['sc']['verdict_list']} in the verdict list). "
         f"Other forums: date known in advance for {s['other_dated']}/{s['other']} (news evidence only).",
         f"- Noise cost: {s['no_move_alerted']} rulings that didn't move the stock were on an "
         f"'any-day' or dated alert.", "",
         "## Alert level before the ruling × what the stock did", "",
         "| Stock reaction | " + " | ".join(LEVELS[k] for k in (3, 2, 1, 0)) + " | Total |",
         "|---|" + "---:|" * 5]
    for key in ("same day", "1-2 days later", "none"):
        row = s["matrix"].get(key, {})
        L.append(f"| {'no unusual move' if key == 'none' else key} | " +
                 " | ".join(str(row.get(k, 0)) for k in (3, 2, 1, 0)) + f" | {sum(row.values())} |")
    L += ["", "## Every ruling", "",
          "T0 = first session that could react. Moves are vs NIFTY.", "",
          "| Date | Stock | Forum | Case | Ruling | T0 | T+1 | T+2 | 3-day | Reaction | Before the ruling | Result |",
          "|---|---|---|---|---|---:|---:|---:|---:|---|---|---|"]
    for e in events:
        rx, sc_ = e.get("rx"), e["score"]
        if not rx:
            continue
        react = "—" if sc_["timing"] == "none" else f"{sc_['timing']} {sc_['impact']}"
        if sc_["as_expected"] is False:
            react += " (opposite)"
        res = "—" if sc_["timing"] == "none" else \
            {"ahead": "✅ alert before", "after": "✅ catchable after", "missed": "❌ missed"}[_bucket(sc_)]
        L.append(f"| {e['ruling_date']} | {e['symbol']} | {e.get('forum', '')} | "
                 f"{(e.get('case') or '').replace('|', '/')} | {e.get('direction', '')} | "
                 f"{_p(rx['r0'])} | {_p(rx['r1'])} | {_p(rx['r2'])} | {_p(rx['cum'])} | {react} | "
                 f"{SHORT[sc_['level']]} — {sc_['basis']} | {res} |")
    miss = [e for e in events if not e.get("rx")]
    if miss:
        L += ["", f"_No usable prices (delisted / renamed / too close to today): "
                  f"{', '.join(sorted({e['symbol'] for e in miss}))}._"]
    L += ["", "## Method & caveats", "",
          "- **Events**: Claude with web search, 9 slices (3 years × Supreme Court / High Courts & "
          "arbitration / tribunals & regulators), told to include rulings with no stock move; then a "
          "second per-ruling research pass for what was public beforehand. Deduped by company ± 3 days.",
          "- **Unusual move**: |abnormal return| ≥ max(3%, 2.5 × the stock's own daily abnormal σ over "
          "the prior 60 sessions) on T0, or the 3-day equivalent with most of it after T0.",
          "- **Supreme Court replay is mechanical**: the real daily + advance schedule PDFs for the ruling "
          "day (still public on api.sci.gov.in) are searched with the live scanner's matcher, by case "
          "number, company and court-recorded party names. A name hitting > 4 items that day is too "
          "generic and only counts inside the verdict list.",
          "- **Other forums**: old schedules aren't online, so 'date known' rests on news evidence.",
          "- **On radar** = dispute public ≥ 7 days earlier. The live tracker only scans F&O stocks and "
          "needs its weekly discovery to have found the case, so this is an upper bound for level 1-2.",
          "- **Bias**: an LLM-compiled event list over-represents rulings that made news. Rulings nobody "
          "reported are absent — they are also the ones least likely to move a stock."]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    out = run(refresh="--refresh" in sys.argv)
    print(json.dumps(out, indent=1, default=str))
