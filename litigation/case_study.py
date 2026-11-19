"""
Yes Bank AT1 case study — could the litigation tracker have caught the
15 Oct 2026 Supreme Court verdict (−14.5% close, −20% intraday)?

Point-in-time replay, using only free data:
  1. BSE filings 2023 → 14 Oct 2026: would the filing scan have flagged the case?
  2. LLM assessment (configured provider, NO web) at three checkpoints, fed only
     facts public on that date: judgment reserved (26 Feb), re-reserved after the
     Cabinet-papers request (20 May), and T-1 (14 Oct, IF the case was seen on the
     next day's pronouncement list).
  3. Trades priced on real NSE F&O bhavcopy settlement prices:
       a. T-1 entries (needs the pronouncement listing): 21 PE, 20 PE, 21 straddle
       b. Same-day entry at the 15 Oct open (ruling came intraday)
       c. Reactive post-verdict drift: short Oct futures at 16 Oct open
       d. "Blind" — no date knowledge: roll ~5%-OTM monthly puts from the day
          judgment was reserved until the verdict expiry (cost of waiting)
  4. Gate: technical BUY signals on YESBANK during the reserved window.

Writes db/litigation_case_study.json (dashboard) + docs/litigation_case_study_yesbank.md.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd

from config import BASE_DIR
from litigation import analyzer as an
from litigation import sources as src
from utils.logger import get_logger

log = get_logger("litigation.case_study")

SYMBOL, SCRIP, LOT = "YESBANK", "532648", 31100
SHARES_CR = 3137.3          # shares outstanding, crore (BSE mcap / price)
VERDICT = date(2026, 10, 15)
OCT_EXPIRY = date(2026, 10, 27)

_BASE_FACTS = (
    "March 2020 RBI-led reconstruction of Yes Bank under s.45 Banking Regulation Act. "
    "On 14 Mar 2020 — one day AFTER the final reconstruction scheme took effect (13 Mar) — "
    "the RBI-appointed administrator wrote down ₹8,415 cr of Basel III AT1 bonds to zero. "
    "The write-down clause present in the DRAFT scheme was omitted from the FINAL notified "
    "scheme. Bondholders (trustee: Axis Trustee) challenged it. 20 Jan 2023: Bombay High "
    "Court Division Bench quashed the write-down, holding the administrator had no "
    "authority once the final scheme took effect (order stayed 6 weeks); stock fell ~8% "
    "next session (alongside weak Q3 results). Yes Bank, RBI and Union of India filed SLPs; "
    "3 Mar 2023 Supreme Court stayed the HC judgment. 19 Feb 2025: SLPs admitted (leave "
    "granted). Reinstatement would restore ₹8,415 cr of liabilities, est. ~250 bps CET1 hit."
)

CHECKPOINTS = [
    {"asof": "2026-02-26", "label": "Judgment reserved", "stage": "reserved",
     "stage_date": "2026-02-26", "next_date": None,
     "facts": _BASE_FACTS + " 26 Feb 2026: final arguments concluded before Justices "
              "Dipankar Datta and A.G. Masih; judgment reserved."},
    {"asof": "2026-05-20", "label": "Re-reserved after Cabinet-papers request", "stage": "reserved",
     "stage_date": "2026-05-20", "next_date": None,
     "facts": _BASE_FACTS + " 26 Feb 2026: arguments concluded, judgment reserved. May 2026: "
              "the Bench asked the Centre to produce the Cabinet resolution, minutes, quorum "
              "and attendance for the meeting that cleared the reconstruction scheme; written "
              "submissions filed; judgment reserved again (~20 May). Yes Bank's disclosed stance: "
              "write-down was per contractual terms; expects no material financial impact."},
    {"asof": "2026-10-14", "label": "T-1: listed for pronouncement (if seen)",
     "stage": "listed_for_pronouncement", "stage_date": "2026-10-14", "next_date": "2026-10-15",
     "facts": _BASE_FACTS + " 26 Feb 2026: arguments concluded, judgment reserved. May 2026: "
              "Bench sought Cabinet papers on the scheme approval; reserved again. Aug 2026: "
              "CRISIL published an estimate of the capital impact of a full reversal. 14 Oct "
              "2026 (evening): the matter appears in the next day's list of judgments to be "
              "pronounced."},
]

TIMELINE = [
    ("2020-03-14", "Write-down", "Administrator writes ₹8,415 cr AT1 bonds to zero, a day after the final scheme took effect."),
    ("2023-01-20", "Bombay HC quashes write-down", "Stock −2.0% that day, −8.1% next session (with weak Q3)."),
    ("2023-03-03", "SC stays HC order", "Appeal pending; risk looks parked."),
    ("2025-02-19", "SLPs admitted", "Final hearing track."),
    ("2026-02-26", "Judgment reserved", "Tracker flips the case HOT → legal-risk flag on every YESBANK signal."),
    ("2026-05-20", "Reserved again", "Bench asks for Cabinet papers — signals scrutiny of the scheme approval."),
    ("2026-10-14", "T-1", "Options priced only ±5.3% to Oct expiry — no event premium."),
    ("2026-10-15", "Verdict: appeals dismissed", "Open ₹21.25, low ₹16.96 (−20%), close ₹18.12 (−14.5%)."),
    ("2026-10-30", "Drift", "₹15.15 — a further −16% in 11 sessions."),
]


def _px() -> pd.Series:
    import yfinance as yf
    df = yf.download(f"{SYMBOL}.NS", start="2022-12-01", end="2026-11-19",
                     progress=False, auto_adjust=False)
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df


def _at(df: pd.DataFrame, d: str, col="Close") -> float:
    i = df.index.searchsorted(pd.Timestamp(d))
    return float(df[col].iloc[min(i, len(df) - 1)])


def _bhav(d: date) -> pd.DataFrame | None:
    for k in range(5):
        b = src.fo_bhavcopy(d - timedelta(days=k))
        if b is not None:
            return b
    return None


def _opt(b: pd.DataFrame, expiry: date, strike: float, typ: str, col="ClsPric") -> float | None:
    o = b[(b.TckrSymb == SYMBOL) & (b.FinInstrmTp == "STO") & (b.OptnTp == typ)
          & (pd.to_datetime(b.XpryDt).dt.date == expiry) & (b.StrkPric == strike)]
    return float(o[col].iloc[0]) if not o.empty else None


def _fut(b: pd.DataFrame, expiry: date, col="ClsPric") -> float | None:
    f = b[(b.TckrSymb == SYMBOL) & (b.FinInstrmTp == "STF") & (pd.to_datetime(b.XpryDt).dt.date == expiry)]
    return float(f[col].iloc[0]) if not f.empty else None


def _trade(name, entry, exit_, px_in, px_out, short=False) -> dict:
    pnl = (px_in - px_out) if short else (px_out - px_in)
    return {"name": name, "entry": entry, "exit": exit_, "px_in": px_in, "px_out": px_out,
            "multiple": None if short or not px_in else px_out / px_in, "pnl_lot": pnl * LOT}


def priced_trades() -> list[dict]:
    t1, d0, d1, exp = date(2026, 10, 14), VERDICT, date(2026, 10, 16), OCT_EXPIRY
    b_t1, b_d0, b_d1, b_exp = _bhav(t1), _bhav(d0), _bhav(d1), _bhav(exp)
    out = []
    for k, typ in ((21.0, "PE"), (20.0, "PE")):
        p_in = _opt(b_t1, exp, k, typ)
        out.append(_trade(f"T-1: buy {k:g} {typ} (Oct)", "14 Oct close", "15 Oct close",
                          p_in, _opt(b_d0, exp, k, typ)))
        out.append(_trade(f"T-1: buy {k:g} {typ} (Oct), hold to expiry", "14 Oct close", "27 Oct expiry",
                          p_in, _opt(b_exp, exp, k, typ)))
    st_in = _opt(b_t1, exp, 21.0, "PE") + _opt(b_t1, exp, 21.0, "CE")
    st_out = _opt(b_d0, exp, 21.0, "PE") + _opt(b_d0, exp, 21.0, "CE")
    out.append(_trade("T-1: long 21 straddle (Oct)", "14 Oct close", "15 Oct close", st_in, st_out))
    out.append(_trade("Same day: buy 21 PE at 15 Oct open", "15 Oct open", "15 Oct close",
                      _opt(b_d0, exp, 21.0, "PE", "OpnPric"), _opt(b_d0, exp, 21.0, "PE")))
    out.append(_trade("Reactive drift: short Oct futures", "16 Oct open", "27 Oct expiry",
                      _fut(b_d1, exp, "OpnPric"), _fut(b_exp, exp, "SttlmPric") or _fut(b_exp, exp),
                      short=True))
    return [t for t in out if t["px_in"] is not None and t["px_out"] is not None]


def blind_put_roll(start: date = date(2026, 2, 26), otm: float = 0.05) -> dict:
    """No date knowledge: from the day judgment was reserved, buy a ~5% OTM put on
    the next monthly expiry (≥5 days out), hold to expiry, roll. Through Oct expiry."""
    legs, d = [], start
    while d <= OCT_EXPIRY:
        b = _bhav(d)
        if b is None:
            break
        o = b[(b.TckrSymb == SYMBOL) & (b.FinInstrmTp == "STO") & (b.OptnTp == "PE")]
        if o.empty:
            break
        trad = pd.to_datetime(o.TradDt.iloc[0]).date()
        exps = sorted({x for x in pd.to_datetime(o.XpryDt).dt.date if (x - trad).days >= 5})
        if not exps:
            break
        exp = exps[0]
        spot = float(o.UndrlygPric.dropna().iloc[0])
        strikes = sorted(o[pd.to_datetime(o.XpryDt).dt.date == exp].StrkPric.unique())
        below = [k for k in strikes if k <= spot * (1 - otm)] or strikes[:1]
        k = below[-1]
        p_in = _opt(b, exp, k, "PE")
        be = _bhav(exp)
        p_out = _opt(be, exp, k, "PE") if be is not None else None
        if p_out is None and be is not None:
            spot_e = be[(be.TckrSymb == SYMBOL) & (be.FinInstrmTp == "STF")].UndrlygPric.dropna()
            p_out = max(k - float(spot_e.iloc[0]), 0.0) if not spot_e.empty else 0.0
        if p_in is None:
            break
        legs.append({"entry": trad.isoformat(), "expiry": exp.isoformat(), "strike": k,
                     "spot": spot, "cost": p_in, "value": p_out or 0.0})
        d = exp + timedelta(days=1)
        if exp >= OCT_EXPIRY:
            break
    cost = sum(l["cost"] for l in legs)
    value = sum(l["value"] for l in legs)
    return {"legs": legs, "total_cost": cost, "total_value": value,
            "pnl_lot": (value - cost) * LOT, "multiple": value / cost if cost else None}


def filing_scan() -> dict:
    """Would the BSE filing scan have surfaced the AT1 case before the verdict?"""
    rows, d = [], date(2023, 1, 1)
    while d < VERDICT:
        e = min(d + timedelta(days=89), VERDICT - timedelta(days=1))
        rows += src.announcements(d, e, scrip=SCRIP)
        d = e + timedelta(days=1)
    legal = src.legal_filings(rows, {SCRIP: SYMBOL}, read_pdf=True)
    flagged = [f for f in legal if f.get("is_legal")]
    at1 = [f for f in flagged if "tier" in (f.get("text", "") + f["subject"]).lower()
           or "at1" in (f.get("text", "") + f["subject"]).lower().replace("-", "")]
    return {"announcements": len(rows), "legal_flagged": len(flagged),
            "at1_mentions": [{"dt": f["dt"][:10], "subject": f["subject"][:140]} for f in at1]}


def gate_hits() -> dict:
    from data.db import get_conn
    try:
        with get_conn() as conn:
            r = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT ss.signal_date) FROM setup_signals ss "
                "JOIN stocks s ON s.id=ss.stock_id WHERE s.symbol=? AND ss.signal_date BETWEEN ? AND ?",
                (f"{SYMBOL}.NS", "2026-02-26", "2026-10-14")).fetchone()
        return {"setup_signals_fired": r[0] or 0, "signal_days": r[1] or 0}
    except Exception as exc:
        return {"error": str(exc)}


def run() -> str:
    from news_analyzer.llm import label
    px = _px()
    out: dict = {"model": label(), "lot": LOT, "generated": datetime.now().isoformat(timespec="seconds")}
    out["timeline"] = [{"date": d, "label": l, "what": w,
                        "price": round(_at(px, d), 2) if d >= "2023-01-01" else None}
                       for d, l, w in TIMELINE]

    log.info("case study: point-in-time assessments")
    assessments = []
    for cp in CHECKPOINTS:
        price = _at(px, cp["asof"])
        r = px["Close"].pct_change()
        hv = float(r[r.index <= cp["asof"]].tail(20).std() * (252 ** 0.5))
        b = _bhav(date.fromisoformat(cp["asof"]))
        snap = src.option_snapshot(SYMBOL, b) if b is not None else None
        case = {"symbol": SYMBOL, "title": "AT1 bond write-down appeals (Yes Bank v Axis Trustee)",
                "court": "Supreme Court of India", "case_number": "SLP(C) 3713/2023 & connected",
                "counterparty": "AT1 bondholders / Axis Trustee", "company_role": "appellant",
                "subject": "Validity of the ₹8,415 cr AT1 write-down quashed by Bombay HC",
                "claim_cr": 8415.0, "stage": cp["stage"], "stage_date": cp["stage_date"],
                "next_date": cp["next_date"], "key_facts": cp["facts"]}
        a = an.assess_case(case, cp["asof"], "Yes Bank Ltd", price, price * SHARES_CR, hv,
                           snap["implied_move"] if snap else None)
        idea = an.trade_idea(case, a, snap, LOT)
        assessments.append({"asof": cp["asof"], "stage": cp["label"], "price": round(price, 2),
                            "implied_move": snap["implied_move"] if snap else None,
                            "materiality": 8415.0 / (price * SHARES_CR),
                            **({k: a[k] for k in ("p_adverse", "move_adverse", "move_favourable",
                                                  "expected_abs_move", "rationale", "confidence")}
                               if a else {}),
                            "trade_idea": idea})
    out["assessments"] = assessments

    log.info("case study: pricing trades on NSE bhavcopy")
    out["trades"] = priced_trades()
    blind = blind_put_roll()
    out["blind_roll"] = blind
    out["trades"].append({"name": f"Blind: roll ~5% OTM monthly puts from 26 Feb ({len(blind['legs'])} legs)",
                          "entry": "26 Feb", "exit": "27 Oct", "px_in": blind["total_cost"],
                          "px_out": blind["total_value"], "multiple": blind["multiple"],
                          "pnl_lot": blind["pnl_lot"]})
    log.info("case study: BSE filing scan")
    out["filings"] = filing_scan()
    out["gate"] = gate_hits()

    a0, a2 = assessments[0], assessments[-1]
    t1put = next((t for t in out["trades"] if t["name"].startswith("T-1: buy 21 PE (Oct)")), None)
    out["verdict"] = (
        f"Partly. The tracker would have flagged YESBANK from 26 Feb 2026 (judgment reserved; "
        f"₹8,415 cr = {a0['materiality']:.0%} of market cap) — 7.5 months before the drop — and put a "
        f"legal-risk line on every technical signal. {out['model']} rated the ruling "
        f"{a0.get('p_adverse', float('nan')):.0%} likely to go against the bank at reservation and "
        f"{a2.get('p_adverse', float('nan')):.0%} at T-1, with an adverse move of "
        f"{a2.get('move_adverse', float('nan')):+.0%}. The options market priced only "
        f"±{(a2.get('implied_move') or 0):.1%} to expiry, so long puts were cheap: the 21 PE bought at "
        f"T-1 returned {t1put['multiple']:.1f}× by the verdict close. The catch is TIMING — the exact "
        f"date is only knowable from the Supreme Court's next-day pronouncement list (captcha-gated; "
        f"needs a manual check or a legal-news hit). Without it, rolling OTM puts from reservation "
        f"returned {blind['multiple']:.2f}× on premium paid. The post-verdict drift (short futures "
        f"from the next open) worked without any foresight."
        if t1put and blind.get("multiple") is not None else "See tables.")
    out["caveats"] = [
        "One event — this is a case study, not a backtest; base rates for SC outcomes are not established here.",
        "The LLM may have absorbed post-as-of information in training; the prompt forbids it but can't guarantee it.",
        "Settlement/close prices ignore bid-ask spread, STT and slippage; YESBANK options are liquid near ATM but far OTM fills are worse.",
        "Lot size held at today's 31,100 for all legs.",
    ]
    (BASE_DIR / "db" / "litigation_case_study.json").write_text(json.dumps(out, indent=1, default=str),
                                                                encoding="utf-8")
    md = _markdown(out)
    (BASE_DIR / "docs" / "litigation_case_study_yesbank.md").write_text(md, encoding="utf-8")
    return md


def _markdown(o: dict) -> str:
    L = ["# Yes Bank AT1 verdict — litigation-tracker case study", "",
         f"_Generated {o['generated']} · assessments by {o['model']}_", "", "## Verdict", "", o["verdict"], "",
         "## Timeline", "", "| Date | Event | Close | Note |", "|---|---|---|---|"]
    L += [f"| {t['date']} | {t['label']} | {t['price'] or ''} | {t['what']} |" for t in o["timeline"]]
    L += ["", "## Point-in-time assessments (no web)", "",
          "| As of | Stage | Price | Options ± | P(adverse) | If adverse | If favourable | Trade idea |",
          "|---|---|---|---|---|---|---|---|"]
    for a in o["assessments"]:
        f = lambda v: "—" if v is None else f"{v:+.0%}"
        L.append(f"| {a['asof']} | {a['stage']} | {a['price']} | "
                 f"{'±%.1f%%' % (a['implied_move']*100) if a.get('implied_move') else '—'} | "
                 f"{a.get('p_adverse', 0):.0%} | {f(a.get('move_adverse'))} | {f(a.get('move_favourable'))} | "
                 f"{a['trade_idea']} |")
    L += ["", "Rationale at T-1: " + (o["assessments"][-1].get("rationale") or ""), "",
          f"## Trades (real NSE settlement prices, 1 lot = {o['lot']:,})", "",
          "| Trade | Entry | Exit | In | Out | Multiple | P&L / lot |", "|---|---|---|---|---|---|---|"]
    for t in o["trades"]:
        L.append(f"| {t['name']} | {t['entry']} | {t['exit']} | {t['px_in']:.2f} | {t['px_out']:.2f} | "
                 f"{'%.1f×' % t['multiple'] if t['multiple'] else '—'} | ₹{t['pnl_lot']:,.0f} |")
    L += ["", "### Blind put roll legs", "", "| Entry | Expiry | Strike | Spot | Cost | Value |", "|---|---|---|---|---|---|"]
    L += [f"| {l['entry']} | {l['expiry']} | {l['strike']:g} | {l['spot']:.2f} | {l['cost']:.2f} | {l['value']:.2f} |"
          for l in o["blind_roll"]["legs"]]
    fs = o["filings"]
    L += ["", "## Would the BSE filing scan have seen it?", "",
          f"{fs['announcements']} announcements Jan 2023 → 14 Oct 2026; {fs['legal_flagged']} flagged legal; "
          f"{len(fs['at1_mentions'])} mention AT1 / Additional Tier-1:", ""]
    L += [f"- {m['dt']}: {m['subject']}" for m in fs["at1_mentions"]] or ["- none"]
    L += ["", "## Signal gate", "", f"Technical setup signals that fired on YESBANK while judgment was reserved (26 Feb → 14 Oct 2026): "
          f"{o['gate'].get('setup_signals_fired')} across {o['gate'].get('signal_days')} trading days — each would have "
          f"carried the ⚖️ legal-risk line (LITIGATION_GATE=flag) or been dropped (=skip).", "",
          "## Caveats", ""] + [f"- {c}" for c in o["caveats"]]
    return "\n".join(L) + "\n"
