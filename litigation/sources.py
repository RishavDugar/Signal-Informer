"""
Free data sources for the litigation tracker. No paid APIs, no captcha bypass.

  BSE corporate announcements (JSON)  — Reg 30 disclosures, incl. litigation /
                                        regulatory orders; filing PDFs → text
  BSE scrip master (JSON)             — NSE symbol → BSE scrip code + market cap
  NSE fo_mktlots.csv                  — F&O universe + lot sizes
  NSE F&O UDiFF bhavcopy (zip/csv)    — option settlement prices → implied move

Supreme Court / eCourts case-status and cause lists sit behind captchas, so they
are deliberately NOT scraped; stage tracking uses the LLM's web search over legal
news (LiveLaw / Bar & Bench / SC Observer / business press) instead.
"""

from __future__ import annotations

import io
import json
import re
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from curl_cffi import requests as cr

from config import BASE_DIR
from utils.logger import get_logger

log = get_logger("litigation.sources")

_CACHE = BASE_DIR / "db" / "cache"
_CACHE.mkdir(parents=True, exist_ok=True)
_BSE_HDRS = {"Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com",
             "Accept": "application/json"}
_NSE_HDRS = {"Referer": "https://www.nseindia.com/"}
_SESSION: cr.Session | None = None


def _s() -> cr.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = cr.Session(impersonate="chrome")
    return _SESSION


def _get(url: str, headers: dict, tries: int = 4, timeout: int = 45, want_json=False):
    last = None
    for k in range(tries):
        try:
            r = _s().get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                if not want_json or r.text.lstrip().startswith(("{", "[")):
                    return r
            last = f"HTTP {r.status_code}"
            if r.status_code == 404:          # missing, not flaky — don't retry
                break
        except Exception as exc:
            last = str(exc)
        if k < tries - 1:
            time.sleep(1.5 + 2.5 * k)
    (log.debug if last == "HTTP 404" else log.warning)(
        f"sources: GET failed — {url[:120]} ({last})")
    return None


def _cached_json(name: str, max_age_h: float, fetch):
    p = _CACHE / name
    if p.exists() and (time.time() - p.stat().st_mtime) < max_age_h * 3600:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    data = fetch()
    if data:
        p.write_text(json.dumps(data), encoding="utf-8")
        return data
    if p.exists():                       # stale beats nothing
        return json.loads(p.read_text(encoding="utf-8"))
    return None


# ── BSE scrip master: symbol ↔ scrip code ↔ market cap ────────────────────────

def bse_scrips() -> dict[str, dict]:
    """{NSE-style symbol: {scrip, name, isin, mcap_cr}} for active BSE equities."""
    def fetch():
        r = _get("https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode="
                 "&industry=&segment=Equity&status=Active", _BSE_HDRS, want_json=True, timeout=90)
        return r.json() if r else None
    raw = _cached_json("bse_scrips.json", 24, fetch) or []
    out = {}
    for e in raw:
        sid = (e.get("scrip_id") or "").upper().strip()
        if not sid:
            continue
        try:
            mcap = float(e.get("Mktcap") or 0) or None
        except ValueError:
            mcap = None
        out[sid] = {"scrip": str(e.get("SCRIP_CD")), "name": e.get("Issuer_Name") or e.get("Scrip_Name"),
                    "isin": e.get("ISIN_NUMBER"), "mcap_cr": mcap}
    return out


def clean(symbol: str) -> str:
    return re.sub(r"\.(NS|BO)$", "", symbol.upper().strip())


def company_info(symbol: str) -> dict:
    return bse_scrips().get(clean(symbol), {})


# ── NSE F&O universe + lot sizes ──────────────────────────────────────────────

def fno_lots() -> dict[str, int]:
    """{SYMBOL: current lot size} for NSE single-stock F&O underlyings."""
    def fetch():
        r = _get("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv", _NSE_HDRS)
        if not r:
            return None
        out = {}
        for line in r.text.splitlines()[1:]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3 or not parts[1] or parts[1] == "Symbol":
                continue
            lot = next((p for p in parts[2:] if p.isdigit()), None)
            if lot:
                out[parts[1].upper()] = int(lot)
        return out
    data = _cached_json("fno_lots.json", 24, fetch) or {}
    idx = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
    return {k: v for k, v in data.items() if k not in idx}


# ── BSE announcements ─────────────────────────────────────────────────────────

_ANN_URL = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno={p}"
            "&strCat=-1&strPrevDate={a}&strScrip={scrip}&strSearch=P&strToDate={b}"
            "&strType=C&subcategory=-1")


def announcements(start: date, end: date, scrip: str = "", max_pages: int = 40) -> list[dict]:
    """Raw BSE announcements in [start, end]; scrip='' = whole market."""
    rows: list[dict] = []
    for p in range(1, max_pages + 1):
        r = _get(_ANN_URL.format(p=p, a=f"{start:%Y%m%d}", b=f"{end:%Y%m%d}", scrip=scrip),
                 _BSE_HDRS, want_json=True)
        if not r:
            break
        t = r.json().get("Table", []) or []
        rows += t
        if len(t) < 50:
            break
        time.sleep(0.4)
    return rows


# Strong legal signals in a filing title/body. Deliberately NOT bare "SEBI" —
# every Reg 30 filing cites the SEBI LODR regulations.
LEGAL_RE = re.compile(
    r"supreme court|high court|hon'?ble (?:court|tribunal|bench)|\bnclt\b|\bnclat\b|"
    r"tribunal|\bsat\b|securities appellate|\bdrt\b|\bitat\b|\bcestat\b|arbitra|"
    r"\bwrit\b|special leave|\bslp\b|civil appeal|judg(?:e)?ment|litigation|"
    r"show[- ]cause|penalt|demand (?:order|notice)|tax demand|adjudicat|"
    r"competition commission|\bcci\b|enforcement directorate|\bcbi\b|"
    r"insolvency|petition|order passed|regulatory action|claim against",
    re.I)
# Titles that are never litigation, even if the body boilerplate matches.
NOISE_RE = re.compile(r"analyst|investor meet|esop|esps|trading window|newspaper|"
                      r"credit rating|esg rating|postal ballot|74 ?\(5\)|loss of share",
                      re.I)


def filing_url(attachment: str) -> list[str]:
    return [f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}",
            f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{attachment}"]


def filing_text(attachment: str, max_pages: int = 6, max_chars: int = 12000) -> str:
    """Download a filing PDF and return its text (first few pages)."""
    if not attachment:
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("sources: pypdf not installed — filing text unavailable (pip install pypdf)")
        return ""
    for url in filing_url(attachment):
        r = _get(url, {"Referer": "https://www.bseindia.com/"}, tries=2, timeout=60)
        if r is None or r.content[:4] != b"%PDF":
            continue
        try:
            rd = PdfReader(io.BytesIO(r.content))
            txt = " ".join((pg.extract_text() or "") for pg in rd.pages[:max_pages])
            return " ".join(txt.split())[:max_chars]
        except Exception as exc:
            log.debug(f"sources: pdf parse failed {attachment}: {exc}")
    return ""


def legal_filings(rows: list[dict], scrip_to_symbol: dict[str, str],
                  read_pdf: bool = True) -> list[dict]:
    """Keep announcements for our universe that look legal. Generic-title filings
    ('Intimation under Regulation 30') are opened and classified on body text —
    titles alone missed every Yes Bank AT1 filing."""
    out = []
    for a in rows:
        scrip = str(a.get("SCRIP_CD") or "")
        sym = scrip_to_symbol.get(scrip)
        if not sym:
            continue
        subject = " ".join(filter(None, [a.get("NEWSSUB"), a.get("HEADLINE")])).strip()
        if NOISE_RE.search(subject):
            continue
        rec = {"news_id": a.get("NEWSID"), "symbol": sym, "scrip": scrip,
               "dt": (a.get("NEWS_DT") or a.get("DT_TM") or "")[:19], "subject": subject[:500],
               "attachment": a.get("ATTACHMENTNAME") or "", "category": a.get("SUBCATNAME")}
        generic = bool(re.search(r"regulation 30|intimation|disclosure|update", subject, re.I))
        if LEGAL_RE.search(subject):
            rec["is_legal"] = True
        elif generic and read_pdf and rec["attachment"]:
            rec["text"] = filing_text(rec["attachment"], max_pages=3, max_chars=6000)
            rec["is_legal"] = bool(LEGAL_RE.search(rec["text"]))
        else:
            rec["is_legal"] = False
        if rec["is_legal"] and read_pdf and "text" not in rec:
            rec["text"] = filing_text(rec["attachment"])
        out.append(rec)
    return out


# ── NSE F&O bhavcopy → option prices / implied move ───────────────────────────

def fo_bhavcopy(d: date) -> pd.DataFrame | None:
    """NSE F&O UDiFF bhavcopy for a trading day (cached on disk)."""
    p = _CACHE / f"fo_{d:%Y%m%d}.pkl"
    if p.exists():
        try:
            return pd.read_pickle(p)
        except Exception:
            pass
    url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    r = _get(url, _NSE_HDRS, tries=2, timeout=90)
    if r is None or r.content[:2] != b"PK":
        return None
    z = zipfile.ZipFile(io.BytesIO(r.content))
    df = pd.read_csv(z.open(z.namelist()[0]))
    df = df[df.FinInstrmTp.isin(["STO", "STF"])][
        ["TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "OpnPric",
         "HghPric", "LwPric", "ClsPric", "SttlmPric", "UndrlygPric", "OpnIntrst", "TtlTradgVol"]]
    try:
        df.to_pickle(p)
    except Exception:
        pass
    return df


def latest_bhavcopy(max_back: int = 7) -> tuple[date | None, pd.DataFrame | None]:
    d = date.today()
    if datetime.now().hour < 19:        # today's file appears in the evening
        d -= timedelta(days=1)
    for _ in range(max_back):
        if d.weekday() < 5:
            df = fo_bhavcopy(d)
            if df is not None:
                return d, df
        d -= timedelta(days=1)
    return None, None


def option_snapshot(symbol: str, bhav: pd.DataFrame, min_days: int = 3) -> dict | None:
    """ATM straddle on the nearest expiry with ≥ min_days to run.
    Returns {spot, expiry, strike, call, put, straddle, implied_move}."""
    s = clean(symbol)
    o = bhav[(bhav.TckrSymb == s) & (bhav.FinInstrmTp == "STO")]
    if o.empty:
        return None
    trad = pd.to_datetime(o.TradDt.iloc[0])
    exps = sorted(pd.to_datetime(o.XpryDt.unique()))
    exps = [e for e in exps if (e - trad).days >= min_days] or exps
    exp = exps[0]
    oe = o[pd.to_datetime(o.XpryDt) == exp]
    spot = float(oe.UndrlygPric.dropna().iloc[0]) if oe.UndrlygPric.notna().any() else None
    if not spot:
        return None
    strikes = sorted(oe.StrkPric.unique())
    k = min(strikes, key=lambda x: abs(x - spot))
    ce = oe[(oe.StrkPric == k) & (oe.OptnTp == "CE")].ClsPric
    pe = oe[(oe.StrkPric == k) & (oe.OptnTp == "PE")].ClsPric
    if ce.empty or pe.empty:
        return None
    straddle = float(ce.iloc[0]) + float(pe.iloc[0])
    return {"spot": spot, "expiry": exp.date().isoformat(), "strike": float(k),
            "call": float(ce.iloc[0]), "put": float(pe.iloc[0]), "straddle": straddle,
            "implied_move": straddle / spot, "days": (exp - trad).days}
