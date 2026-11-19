"""
Supreme Court verdict census — every listed company named in an SC verdict list,
every working day for three years, straight from the court's own PDFs.

The LLM-compiled event list in litigation.backtest is only as complete as web
search (it missed Yes Bank's AT1 verdict). This census is mechanical and complete
for what it covers: SC verdicts in which a listed company (F&O or market cap
≥ ₹5,000 cr) is a named party. It answers two questions:

  1. How much would the live verdict watch (litigation.verdicts) have flagged —
     and how many of those verdicts actually moved the stock?
  2. When was each list signed? (the registrar's timestamp on the PDF) — i.e.
     was the alert really available the evening before?

    python -m litigation.census            # resumable; caches one small JSON per day
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from utils.logger import get_logger

log = get_logger("litigation.census")

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "db" / "cache" / "backtest" / "census"
# Registrar ('M_R') lists are procedural; advance lists carry no verdicts.
CODES = ["M_J_1", "M_J_2", "M_J_3", "F_J_1", "F_J_2", "F_J_3", "M_C_1", "M_C_2", "M_S_1", "M_CC_1"]


CANARY = "https://api.sci.gov.in/jonew/cl/2026-10-15/F_J_1.pdf"     # a list known to exist


def _blocked() -> bool:
    """The SC server answers '404' to everything once it rate-limits a client —
    so a known-good list failing means we're blocked, not that lists are missing."""
    from litigation.sources import _get
    r = _get(CANARY, {"Referer": "https://www.sci.gov.in/"}, tries=1, timeout=45)
    return r is None or r.content[:4] != b"%PDF"


def _wait_unblocked() -> None:
    while _blocked():
        log.warning("census: SC server is rate-limiting us — pausing 15 min")
        time.sleep(900)


def _day(d: date) -> dict:
    path = CACHE / f"{d.isoformat()}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    _wait_unblocked()
    from litigation.attachments import extract_text
    from litigation.matching import published_at, verdict_hits
    from litigation.sources import _get
    out = {"date": d.isoformat(), "lists": 0, "published": None, "hits": [], "errors": 0}
    tmp_dir = CACHE / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for code in CODES:
        u = f"https://api.sci.gov.in/jonew/cl/{d.isoformat()}/{code}.pdf"
        r = _get(u, {"Referer": "https://www.sci.gov.in/"}, tries=2, timeout=60)
        if r is None:
            out["errors"] += 1
            continue
        if r.content[:4] != b"%PDF":
            continue
        tmp = tmp_dir / f"{d.isoformat()}_{code}.pdf"
        tmp.write_bytes(r.content)
        try:
            txt = extract_text(tmp)
        except Exception:
            txt = ""
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        if not txt:
            continue
        out["lists"] += 1
        pub = published_at(txt)
        if pub and (out["published"] is None or pub < out["published"]):
            out["published"] = pub
        for h in verdict_hits(txt, sc=True):
            h.pop("pos", None)
            out["hits"].append({**h, "list": code})
        time.sleep(1.0)
    # Blocked part-way through this day? Then its result is incomplete: don't cache,
    # a later pass re-fetches it. (A real court holiday — all 404, server fine — is cached.)
    if _blocked():
        return out
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def run(start: date = date(2023, 11, 1), end: date = date(2026, 11, 18), threads: int = 2) -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    todo = [x for x in days if not (CACHE / f"{x.isoformat()}.json").exists()]
    log.info(f"census: {len(days)} working days, {len(todo)} to fetch")
    done = 0
    with ThreadPoolExecutor(threads) as ex:
        for _ in ex.map(_day, todo):
            done += 1
            if done % 50 == 0:
                log.info(f"census: {done}/{len(todo)} days fetched")
    return [_day(x) for x in days]


if __name__ == "__main__":
    res = run()
    n = sum(len(r["hits"]) for r in res)
    print(f"{len(res)} days, {sum(1 for r in res if r['lists'])} with lists, {n} listed-company verdict items")
