"""
Litigation tracker — orchestration, alerts, signal gate, CLI.

Daily run (evening task, LITIGATION_SCHEDULE_HOUR:MINUTE):
  1. BSE filings since the last run for the universe → legal ones read + classified
  2. Refresh every active material case (web) — catches reserved / listed / decided
  3. Rolling web discovery of the stalest N symbols (whole F&O universe ≈ weekly)
  4. Re-assess changed cases, attach live option prices → trade idea
  5. WhatsApp alert for new material cases and stage changes

Market sweep (upcoming week): themed web searches across courts/regulators for
rulings expected in the next 7 days or reserved and due any day.

CLI:
  python -m litigation.tracker daily
  python -m litigation.tracker sweep [--days 7] [--top 60]
  python -m litigation.tracker discover YESBANK RELIANCE ...
  python -m litigation.tracker report YESBANK
  python -m litigation.tracker casestudy
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import numpy as np

from litigation import analyzer as an
from litigation import db as ldb
from litigation import sources as src
from litigation.db import CLOSED_STAGES, HOT_STAGES
from utils.logger import get_logger

log = get_logger("litigation")


# ── Universe + market data helpers ────────────────────────────────────────────

def universe(top: int | None = None) -> list[str]:
    """Symbols to track (clean NSE tickers), largest market cap first."""
    from config import LITIGATION_UNIVERSE
    scrips = src.bse_scrips()
    if LITIGATION_UNIVERSE == "db":
        from data.db import get_active_stocks
        syms = [src.clean(s["symbol"]) for s in get_active_stocks()]
    else:
        syms = list(src.fno_lots().keys())
    syms = [s for s in syms if s in scrips]
    syms.sort(key=lambda s: scrips[s].get("mcap_cr") or 0, reverse=True)
    return syms[:top] if top else syms


def _price_and_vol(symbol: str, asof: str | None = None) -> tuple[float | None, float | None]:
    """Last close and 20-day realised vol (annualised) from DB, else yfinance."""
    try:
        from data.db import get_ohlcv
        df = get_ohlcv(f"{symbol}.NS", days=60)
        c = df["close"] if df is not None and not df.empty else None
    except Exception:
        c = None
    if c is None or len(c) < 21:
        try:
            import yfinance as yf
            h = yf.Ticker(f"{symbol}.NS").history(period="3mo", auto_adjust=False)
            c = h["Close"]
        except Exception:
            return None, None
    if c is None or len(c) < 21:
        return None, None
    if asof:
        c = c[c.index <= str(asof)]
    r = c.pct_change().dropna()
    return float(c.iloc[-1]), float(r.tail(20).std() * np.sqrt(252))


_BHAV: tuple | None = None


def _snapshot(symbol: str) -> dict | None:
    global _BHAV
    if _BHAV is None:
        _BHAV = src.latest_bhavcopy()
    d, df = _BHAV
    return src.option_snapshot(symbol, df) if df is not None else None


# ── Assess + store ────────────────────────────────────────────────────────────

def assess_and_store(symbol: str, case: dict, case_id: int | None = None) -> dict:
    """Assess one case (point-in-time = today), attach option data + trade idea,
    persist. Returns the case dict enriched with the assessment."""
    info = src.company_info(symbol)
    mcap = info.get("mcap_cr")
    price, hv = _price_and_vol(symbol)
    snap = _snapshot(symbol) if case.get("stage") not in CLOSED_STAGES else None
    a = an.assess_case({**case, "symbol": symbol}, date.today().isoformat(),
                       info.get("name") or symbol, price, mcap, hv,
                       snap["implied_move"] if snap else None)
    lot = src.fno_lots().get(symbol)
    idea = an.trade_idea(case, a, snap, lot)
    rec = {"mcap_cr": mcap, "materiality": an.materiality(case, mcap),
           "implied_move": snap["implied_move"] if snap else None, "trade_idea": idea}
    if a:
        rec.update({k: a[k] for k in ("p_adverse", "move_adverse", "move_favourable",
                                      "expected_abs_move", "confidence")})
        drivers = "; ".join(a.get("key_drivers") or [])
        rec["rationale"] = (a.get("rationale") or "") + (f" Drivers: {drivers}." if drivers else "") + \
            (f" Ruling window: {a['ruling_window']}." if a.get("ruling_window") else "")
    if case_id is None:
        case_id, _ = ldb.upsert_case(symbol, case)
    ldb.save_assessment(case_id, rec)
    return {**case, **rec, "id": case_id, "symbol": symbol, "material": an.is_material(case, a, mcap)}


def _ingest_case(symbol: str, case: dict, source: str) -> dict | None:
    """Upsert a discovered case, log new/stage-change events, assess if new or
    changed. Immaterial new cases are stored but closed (kept for dedup)."""
    if not case.get("company_name"):
        case["company_name"] = src.company_info(symbol).get("name")
    cid, prev = ldb.upsert_case(symbol, case)
    changed = prev is None or (case.get("stage") not in (None, "unknown")
                               and case.get("stage") != prev.get("stage")) \
        or (case.get("outcome") and case.get("outcome") != prev.get("outcome")) \
        or (case.get("next_date") and case.get("next_date") != prev.get("next_date"))
    if not changed and prev and prev.get("last_assessed"):
        return None
    out = assess_and_store(symbol, {**(prev or {}), **case}, cid)
    if not out["material"]:
        if prev is None or prev.get("status") == "immaterial":
            ldb.execute("UPDATE lit_cases SET status='immaterial' WHERE id=?", (cid,))
        return None
    if prev is not None and prev.get("status") == "immaterial":   # became material
        ldb.execute("UPDATE lit_cases SET status=? WHERE id=?",
                    ("closed" if out.get("stage") in CLOSED_STAGES else "active", cid))
        prev = None                                                 # alert as new
    if prev is None:
        ldb.add_event(cid, symbol, "new_case", None, case.get("stage"),
                      f"{source}: {case.get('subject') or case.get('title')}")
    elif case.get("stage") != prev.get("stage"):
        ldb.add_event(cid, symbol, "stage_change", prev.get("stage"), case.get("stage"),
                      case.get("update") or case.get("outcome") or "")
    elif case.get("next_date") != prev.get("next_date"):
        ldb.add_event(cid, symbol, "date_change", prev.get("next_date"), case.get("next_date"),
                      case.get("update") or "")
    return out


# ── Pipeline steps ────────────────────────────────────────────────────────────

def scan_filings(days: int = 3) -> int:
    """Market-wide BSE announcements for the last `days` → legal filings for the
    universe → LLM classification → cases."""
    scrips = src.bse_scrips()
    uni = set(universe())
    scrip_to_sym = {v["scrip"]: k for k, v in scrips.items() if k in uni}
    end = date.today()
    rows = src.announcements(end - timedelta(days=days), end)
    rows = [r for r in rows if not ldb.seen_filing(str(r.get("NEWSID")))]
    found = 0
    for f in src.legal_filings(rows, scrip_to_sym):
        f["news_id"] = str(f["news_id"])
        case = None
        if f.get("is_legal"):
            info = scrips.get(f["symbol"], {})
            case = an.classify_filing(f, info.get("name"), info.get("mcap_cr"))
            if case:
                case.setdefault("sources", [])
                case["sources"] = list(case["sources"]) + src.filing_url(f["attachment"])[:1]
                if _ingest_case(f["symbol"], case, f"BSE filing {f['dt'][:10]}"):
                    found += 1
        ldb.save_filing({**f, "processed": True})
    log.info(f"filings: {len(rows)} new announcements scanned, {found} material case update(s)")
    return found


def refresh_active(only_hot: bool = False) -> int:
    """Web-refresh open cases: material + near-threshold ones, plus any lower-tier
    case that is reserved / dated within a week (so its colour can escalate).
    Hot (reserved/listed) or dated-this-week daily; others when 3+ days stale."""
    cutoff = (datetime.now() - timedelta(days=3)).isoformat()
    soon = (date.today() + timedelta(days=7)).isoformat()
    pool = ldb.active_cases() + [
        c for c in ldb.query("SELECT * FROM lit_cases WHERE status='immaterial' AND "
                             "stage NOT IN ('decided','disposed')")
        if an.tier(c)[0] == "near" or c.get("stage") in HOT_STAGES
        or (c.get("next_date") and date.today().isoformat() <= c["next_date"] <= soon)]
    todo = [c for c in pool
            if c.get("stage") in HOT_STAGES or (not only_hot and (c.get("last_checked") or "") < cutoff)
            or (c.get("next_date") and c["next_date"] <= soon)]
    n = 0

    def _one(c):
        info = src.company_info(c["symbol"])
        return c, an.refresh_case(c, info.get("name", ""))

    with ThreadPoolExecutor(max_workers=3) as ex:
        for fut in as_completed([ex.submit(_one, c) for c in todo]):
            c, upd = fut.result()
            if upd and _ingest_case(c["symbol"], {**upd, "case_key": c["case_key"]}, "refresh"):
                n += 1
    log.info(f"refresh: {len(todo)} case(s) checked, {n} changed")
    return n


def discover(symbols: list[str]) -> int:
    scrips = src.bse_scrips()
    n = 0

    def _one(sym):
        info = scrips.get(sym, {})
        filings = ldb.query("SELECT * FROM lit_filings WHERE symbol=? AND is_legal=1 "
                            "ORDER BY dt DESC LIMIT 8", (sym,))
        return sym, an.discover_cases(sym, info.get("name"), info.get("mcap_cr"), filings)

    with ThreadPoolExecutor(max_workers=3) as ex:
        for fut in as_completed([ex.submit(_one, s) for s in symbols]):
            sym, cases = fut.result()
            kept = sum(1 for c in cases if _ingest_case(sym, c, "web discovery"))
            ldb.mark_discovered(sym, kept)
            n += kept
    log.info(f"discover: {len(symbols)} symbol(s), {n} material case(s)")
    return n


def rolling_discovery() -> int:
    from config import LITIGATION_DISCOVERY_PER_RUN, LITIGATION_REDISCOVER_DAYS
    last = ldb.last_discovered()
    stale_before = (datetime.now() - timedelta(days=LITIGATION_REDISCOVER_DAYS)).isoformat()
    todo = [s for s in universe() if last.get(s, "") < stale_before]
    todo.sort(key=lambda s: last.get(s, ""))
    return discover(todo[:LITIGATION_DISCOVERY_PER_RUN])


_SWEEP_THEMES = [
    "Supreme Court of India: judgments reserved or listed for pronouncement, and final hearings, in cases involving listed companies",
    "High Courts (Bombay, Delhi, Madras, Gujarat, Karnataka, Calcutta): verdicts expected or reserved in cases involving listed companies",
    "NCLT / NCLAT: insolvency, oppression/mismanagement, merger-scheme objections involving listed companies",
    "SEBI orders and Securities Appellate Tribunal (SAT) appeals involving listed companies or promoters",
    "Competition Commission of India (CCI) penalties/investigations and NCLAT competition appeals involving listed companies",
    "Large tax disputes (income tax, GST, customs) and ITAT/CESTAT/Supreme Court tax rulings involving listed companies",
    "Sector regulators and tribunals — TDSAT (telecom AGR/spectrum), APTEL/CERC (power tariffs), mining/environment (NGT), pharma (USFDA/DCGI litigation), RBI-related bank cases",
    "International arbitration and foreign court cases (US DOJ/SEC, SIAC, LCIA) involving Indian listed companies",
]

_SWEEP_PROMPT = """\
Today is {today}. Window: {start} to {end}.

THEME: {theme}

Using web search (LiveLaw, Bar & Bench, SC Observer, Moneycontrol, Economic Times,
Business Standard, Reuters, exchange filings), build a list of legal/regulatory
proceedings involving companies LISTED ON NSE (large caps and F&O stocks first) that
fall in ANY of these buckets:
  A. a ruling / pronouncement / final hearing is scheduled or expected within the window
  B. judgment or order is RESERVED (or arguments concluded) and could land any day —
     include these even if no date is known, however long ago it was reserved
  C. a ruling was delivered in the last 5 days and the stock may still be reacting
  D. a significant hearing happened in the last 30 days and the matter is live
Be thorough: run AT LEAST 6 differently-phrased searches (e.g. "verdict reserved
<theme> listed company", "SC reserves judgment shares", "hearing next week <theme>",
"order reserved NCLAT", "ruling today shares fall court") before answering. A
matter with an unknown date is still useful — return it with expected_date null.
Include anything that could plausibly move the share price by 3% or more; up to 10.

Return ONLY JSON:
{{"cases": [ {{"symbol": "NSE ticker e.g. YESBANK", "company": "name", "expected_date": "YYYY-MM-DD or null",
              "why_this_week": "one line", "case": {schema} }} ]}}
Return {{"cases": []}} if none. Never invent dates or case numbers — use null.
"""


def sweep_upcoming(days: int = 7, top: int = 60) -> list[dict]:
    """Find material cases with a ruling/hearing expected in the next `days` days:
    themed market-wide web sweeps + per-company discovery for the `top` largest
    F&O names. Everything found is assessed and stored (visible in the UI)."""
    sweep_themes(days)
    if top:
        discover(universe(top))
    return upcoming(days)


def sweep_themes(days: int = 7) -> int:
    """Themed market-wide web sweeps only (courts/regulators × listed companies)."""
    from news_analyzer.llm import extract_json, generate
    start, end = date.today(), date.today() + timedelta(days=days)
    valid = set(src.bse_scrips())
    found: list[tuple[str, dict]] = []

    def _theme(theme):
        p = _SWEEP_PROMPT.format(today=start.isoformat(), start=start.isoformat(),
                                 end=end.isoformat(), theme=theme, schema=an._CASE_SCHEMA)
        try:
            d = extract_json(generate(p, timeout=900, think=True, web=True, max_turns=28)) or {}
        except Exception as exc:
            log.warning(f"sweep: theme failed — {exc}")
            return []
        return d.get("cases", []) if isinstance(d, dict) else []

    with ThreadPoolExecutor(max_workers=3) as ex:
        for fut in as_completed([ex.submit(_theme, t) for t in _SWEEP_THEMES]):
            for item in fut.result():
                sym = src.clean(str(item.get("symbol") or ""))
                case = item.get("case") if isinstance(item.get("case"), dict) else None
                if not sym or sym not in valid or not case:
                    continue
                case = an._norm_case(case)
                if not case.get("next_date") and item.get("expected_date"):
                    case["next_date"] = an._norm_case({"next_date": item["expected_date"]})["next_date"]
                case["key_facts"] = ((case.get("key_facts") or "") + " This week: " +
                                     str(item.get("why_this_week") or "")).strip()
                found.append((sym, case))
    log.info(f"sweep: {len(found)} raw case(s) from {len(_SWEEP_THEMES)} themes")
    kept = sum(1 for sym, case in found if _ingest_case(sym, case, "weekly sweep"))
    log.info(f"sweep: {kept} material case(s) stored/updated")
    return kept


def dedupe() -> int:
    """Mark re-worded duplicates of the same case (keeps the active / most recently
    assessed row). Cheap, no LLM."""
    rows = ldb.query("SELECT * FROM lit_cases WHERE status!='duplicate' ORDER BY symbol, "
                     "CASE status WHEN 'active' THEN 0 WHEN 'closed' THEN 1 ELSE 2 END, "
                     "COALESCE(last_assessed,'') DESC")
    kept: dict[str, list[dict]] = {}
    n = 0
    for r in rows:
        mine = ldb._tokens(r)
        dup = any(len(mine & ldb._tokens(k)) / max(1, len(mine | ldb._tokens(k))) >= 0.5
                  for k in kept.get(r["symbol"], []))
        if dup:
            ldb.execute("UPDATE lit_cases SET status='duplicate' WHERE id=?", (r["id"],))
            n += 1
        else:
            kept.setdefault(r["symbol"], []).append(r)
    log.info(f"dedupe: {n} duplicate case row(s) marked")
    return n


def recheck_materiality() -> int:
    """Re-apply the current materiality rule to stored cases (cheap, no LLM).
    Demotes cases that no longer qualify; returns the number demoted."""
    n = 0
    for c in ldb.query("SELECT * FROM lit_cases WHERE status IN ('active','closed')"):
        a = None if c.get("p_adverse") is None else {"expected_abs_move": c.get("expected_abs_move") or 0}
        if not an.is_material(c, a, c.get("mcap_cr")):
            ldb.execute("UPDATE lit_cases SET status='immaterial' WHERE id=?", (c["id"],))
            n += 1
    log.info(f"recheck: {n} case(s) demoted to immaterial")
    return n


def upcoming(days: int = 7) -> list[dict]:
    """Active material cases whose ruling is due within `days` or is reserved/listed."""
    end = (date.today() + timedelta(days=days)).isoformat()
    rows = ldb.query("SELECT * FROM lit_cases WHERE status='active' AND "
                     "(stage IN ('reserved','listed_for_pronouncement','arguments_concluded') "
                     " OR (next_date IS NOT NULL AND next_date BETWEEN ? AND ?))",
                     (date.today().isoformat(), end))
    rows.sort(key=lambda r: (-(r.get("expected_abs_move") or 0)))
    return rows


# ── Alerts ────────────────────────────────────────────────────────────────────

from litigation.plain import STAGE as _STAGE_TXT


def _fmt_case(e: dict) -> str:
    sym = e.get("symbol")
    head = {"new_case": "🆕 New high-impact case", "stage_change": "🔔 Case moved forward",
            "date_change": "📅 New hearing date", "document": "📎 Your document read"}.get(e.get("kind"), "⚖️ Update")
    lines = [f"*{sym}* — {head}", f"_{e.get('title') or ''}_ · {e.get('court') or ''}"]
    if e.get("kind") == "stage_change":
        lines.append(f"{_STAGE_TXT.get(e.get('old_value'), e.get('old_value'))} → "
                     f"*{_STAGE_TXT.get(e.get('new_value'), e.get('new_value'))}*")
    if e.get("next_date"):
        lines.append(f"Next date: {e['next_date']}")
    if e.get("claim_cr"):
        m = f" ({e['materiality']:.0%} of company value)" if e.get("materiality") else ""
        lines.append(f"Money at stake: ₹{e['claim_cr']:,.0f} cr{m}")
    if e.get("p_adverse") is not None:
        lines.append(f"Chance the court rules against the company: {e['p_adverse']:.0%} · stock move if "
                     f"against {e['move_adverse']:+.0%}, if for {e['move_favourable']:+.0%}")
    if e.get("implied_move"):
        lines.append(f"Move the options market already expects: ±{e['implied_move']:.1%}")
    if e.get("trade_idea"):
        lines.append(f"💡 {e['trade_idea']}")
    if e.get("detail"):
        lines.append(f"_{e['detail'][:200]}_")
    return "\n".join(lines)


def send_alerts() -> int:
    from config import LITIGATION_GROUP, NOTIFY_ON_SIGNAL
    evs = ldb.unalerted_events()
    if not evs:
        return 0
    if not NOTIFY_ON_SIGNAL:
        ldb.mark_alerted([e["id"] for e in evs])
        return 0
    from config import LITIGATION_DASHBOARD_URL
    from litigation.insight import one_liner
    rows = {r["id"]: r for r in ldb.query("SELECT * FROM lit_cases")}
    icon = {"material": "🔴", "near": "🟠", "below": "⚪"}
    lines, seen = [], set()
    for e in evs:
        c = rows.get(e.get("case_id"))
        if not c or e.get("kind") == "document" or c["id"] in seen:
            continue                              # your own uploads aren't news to you
        seen.add(c["id"])
        t = an.tier(c)[0]
        lead = {"new_case": "New case: ",
                "stage_change": f"Update ({_STAGE_TXT.get(e.get('new_value'), e.get('new_value'))}): ",
                "date_change": f"New hearing date {e.get('new_value')}: "}.get(e.get("kind"), "")
        lines.append(f"{icon.get(t, '⚪')} {lead}{one_liner(c, t)}")
    if not lines:
        ldb.mark_alerted([e["id"] for e in evs])
        return 0
    msgs = ["\n".join([f"*⚖️ Legal heads-up — {date.today():%a %d %b}*", ""] + lines +
                      ["", f"Details & links: {LITIGATION_DASHBOARD_URL}"])]
    if not LITIGATION_GROUP:
        log.info("alerts: LITIGATION_GROUP not set — not sending")
        return 0
    ok = ldb.wa_send(msgs)
    if ok is None:                 # WhatsApp not up → do nothing; events stay unalerted
        return 0
    if ok:
        ldb.mark_alerted([e["id"] for e in evs])
    log.info(f"alerts: {len(evs)} event(s) → {'sent' if ok else 'SEND FAILED'}")
    return len(evs)


# ── Signal gate + reports ─────────────────────────────────────────────────────

def legal_risk(symbol: str) -> dict | None:
    """The most important hot (ruling-imminent) material case for a symbol, or None.
    Used by the technical pipeline gate. Never raises."""
    try:
        sym = src.clean(symbol)
        for c in ldb.cases_for(sym):
            if c.get("stage") in HOT_STAGES:
                return c
    except Exception as exc:
        log.debug(f"legal_risk({symbol}) failed: {exc}")
    return None


def report(symbol: str, refresh: bool = True) -> str:
    """WhatsApp-ready legal report for one stock (used by 'Legal SYMBOL')."""
    sym = src.clean(symbol)
    if refresh:
        discover([sym])
    cases = ldb.cases_for(sym)
    if not cases:
        return f"*⚖️ Legal check — {sym}*\nNo material pending litigation found."
    out = [f"*⚖️ Legal check — {sym}*", ""]
    for c in cases[:5]:
        out += ["━━━━━━━━━━━━━━", _fmt_case({**c, "kind": "", "detail": c.get("rationale")})]
        try:
            srcs = json.loads(c.get("sources") or "[]")[:2]
            out += [f"🔗 {u}" for u in srcs]
        except Exception:
            pass
    return "\n".join(out)


# ── Entry points ──────────────────────────────────────────────────────────────

def run_daily() -> None:
    from config import LITIGATION_ENABLED
    if not LITIGATION_ENABLED:
        log.info("litigation: disabled (LITIGATION_ENABLED=false)")
        return
    from news_analyzer.llm import ensure_available, warmup_model
    if not (ensure_available() and warmup_model()):
        log.error("litigation: LLM unavailable — aborting")
        return
    ldb.init()
    log.info("=" * 60 + "\nlitigation: daily run")
    from litigation.attachments import auto_scan_public_lists
    from litigation.identify import identify          # search recipes for new cases
    for step in (scan_filings, auto_scan_public_lists, refresh_active, rolling_discovery,
                 dedupe, recheck_materiality, identify):
        try:
            step()
        except Exception as exc:
            log.exception(f"litigation: step {step.__name__} failed — {exc}")
    send_alerts()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="litigation.tracker")
    ap.add_argument("cmd", nargs="?", default="daily",
                    choices=["daily", "sweep", "themes", "discover", "report", "refresh", "alerts", "casestudy"])
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--top", type=int, default=60)
    a = ap.parse_args(argv)
    from data.db import init_db
    init_db()
    ldb.init()
    if a.cmd == "daily":
        run_daily()
    elif a.cmd == "sweep":
        rows = sweep_upcoming(a.days, a.top)
        for r in rows:
            print(f"{r['symbol']:12} {r.get('stage'):26} next={r.get('next_date')}  "
                  f"P(adv)={r.get('p_adverse')}  {r.get('title')}")
    elif a.cmd == "themes":
        print(f"themes: {sweep_themes(a.days)} material case(s)")
    elif a.cmd == "discover":
        discover([src.clean(s) for s in a.symbols] or universe(a.top))
    elif a.cmd == "report":
        print(report(a.symbols[0]))
    elif a.cmd == "refresh":
        refresh_active()
    elif a.cmd == "alerts":
        send_alerts()
    elif a.cmd == "casestudy":
        from litigation.case_study import run
        print(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
