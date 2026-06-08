"""
Multi-source news fetcher for Indian stock market.

Fetches from:
  - 18 curated RSS feeds (ET, MoneyControl, LiveMint, Business Standard, etc.)
  - 22 Google News RSS topic queries (sectors, M&A, corporate actions, etc.)
  - NSE India event calendar (upcoming corporate actions with session handling)

All fetching is parallel; articles normalised to a common dict shape and
deduplicated by URL.  Only articles published within the last 48 hours
are returned so the LLM only sees fresh, actionable news.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

import requests

from utils.logger import get_logger

log = get_logger("news_fetcher")

_TIMEOUT   = 12   # seconds per request
_MAX_AGE_H = 168  # keep articles from last 7 days (bonus/M&A news is actionable all week)

# ── RSS Feeds ─────────────────────────────────────────────────────────────────
# 18 Indian financial news sources covering equities, economy, sectors, M&A.

RSS_FEEDS: dict[str, str] = {
    # Economic Times — most comprehensive, multiple verticals
    "ET Markets"  : "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "ET Stocks"   : "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "ET MnA"      : "https://economictimes.indiatimes.com/topic/mergers-and-acquisitions/rssfeeds/1557946.cms",
    "ET Earnings" : "https://economictimes.indiatimes.com/markets/earnings/rssfeeds/2143776.cms",
    "ET Economy"  : "https://economictimes.indiatimes.com/economy/rssfeeds/1373380680.cms",
    "ET IPO"      : "https://economictimes.indiatimes.com/markets/ipos/fpos/rssfeeds/6160102.cms",

    # MoneyControl
    "MC Markets"  : "https://www.moneycontrol.com/rss/marketsnews.xml",
    "MC Business" : "https://www.moneycontrol.com/rss/business.xml",

    # LiveMint
    "Mint Markets"   : "https://www.livemint.com/rss/markets",
    "Mint Companies" : "https://www.livemint.com/rss/companies",
    "Mint Economy"   : "https://www.livemint.com/rss/economy",

    # Business Standard
    "BS Markets"   : "https://www.business-standard.com/rss/markets-106.rss",
    "BS Companies" : "https://www.business-standard.com/rss/companies-101.rss",
    "BS Economy"   : "https://www.business-standard.com/rss/economy-102.rss",

    # Others
    "FE Markets"  : "https://www.financialexpress.com/market/feed/",
    "CNBCTV18"    : "https://www.cnbctv18.com/commonfeeds/v1/eng/rss/market.xml",
    "BL Markets"  : "https://www.thehindubusinessline.com/markets/?service=rss",
    "Zee Biz"     : "https://www.zeebiz.com/markets/rss",
}

# ── Google News RSS Queries ───────────────────────────────────────────────────
# 22 topic queries cover all major sectors, corporate actions, and events.
# Each returns up to ~100 fresh articles from aggregated Indian news sources.

_GN_BASE = "https://news.google.com/rss/search?hl=en-IN&gl=IN&ceid=IN:en&q="

GOOGLE_NEWS_QUERIES: list[str] = [
    # Corporate actions (highest signal for stock picks)
    "NSE BSE India stock bonus dividend announcement",
    "India stock buyback open offer announcement",
    "India merger acquisition deal NSE BSE",
    "India corporate demerger restructuring spin-off",

    # Earnings & fundamentals
    "India quarterly results earnings beats NSE BSE",
    "India annual report guidance upgrade outlook",
    "India analyst upgrade target price raise",

    # Sector-specific
    "India pharma healthcare USFDA approval drug",
    "India banking NBFC credit growth NPA results",
    "India IT software technology order win contract",
    "India infrastructure construction order book",
    "India auto automobile EV sales volume",
    "India FMCG consumer staples volume growth",
    "India steel metals commodity export order",
    "India telecom 5G spectrum data growth",
    "India oil gas energy capex production",
    "India real estate housing demand launch",
    "India defence aerospace HAL BEL order win",

    # Capital flows & macro
    "FII DII India block deal bulk deal buying",
    "India IPO listing SME mainboard",
    "India government policy sector stimulus budget",
    "Nifty 50 Sensex stock market rally outlook",
]

# ── NSE Event Calendar ────────────────────────────────────────────────────────

_NSE_MAIN   = "https://www.nseindia.com"
_NSE_EVENTS = "https://www.nseindia.com/api/event-calendar"
_NSE_HDRS   = {
    "User-Agent"      : ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0.0.0 Safari/537.36"),
    "Accept"          : "application/json, text/plain, */*",
    "Accept-Language" : "en-US,en;q=0.9",
    "Referer"         : "https://www.nseindia.com/",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_pubdate(s: str) -> datetime | None:
    """Parse RFC-2822 (RSS) or ISO date strings into UTC-aware datetime."""
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    # Atom / ISO fallback
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:19], fmt[:len(s[:19])])
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _is_recent(pub_str: str, max_age_hours: int = _MAX_AGE_H) -> bool:
    dt = _parse_pubdate(pub_str)
    if dt is None:
        return True   # unknown age: keep it
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    return dt >= cutoff


def _parse_rss(content: str, source: str) -> list[dict]:
    """
    Parse RSS 2.0 or Atom XML into normalised article dicts.
    Uses stdlib xml.etree — no feedparser dependency.
    """
    articles: list[dict] = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return articles

    # Detect Atom namespace
    ns_atom = "http://www.w3.org/2005/Atom"

    # RSS 2.0 items
    for item in root.iter("item"):
        title   = _strip_html(item.findtext("title",       ""))
        summary = _strip_html(item.findtext("description", ""))[:400]
        link    = (item.findtext("link") or "").strip()
        pubdate = (item.findtext("pubDate") or "").strip()
        if not title:
            continue
        articles.append({"title": title, "summary": summary,
                          "link": link, "source": source, "published": pubdate})

    # Atom <entry>
    for entry in root.iter(f"{{{ns_atom}}}entry"):
        title   = _strip_html(entry.findtext(f"{{{ns_atom}}}title",   ""))
        summary = _strip_html(entry.findtext(f"{{{ns_atom}}}summary", ""))[:400]
        link_el = entry.find(f"{{{ns_atom}}}link")
        link    = (link_el.get("href", "") if link_el is not None else "").strip()
        pubdate = (entry.findtext(f"{{{ns_atom}}}updated") or
                   entry.findtext(f"{{{ns_atom}}}published") or "").strip()
        if not title:
            continue
        articles.append({"title": title, "summary": summary,
                          "link": link, "source": source, "published": pubdate})

    return articles


# ── Per-source fetch functions ────────────────────────────────────────────────

def _fetch_rss(name: str, url: str) -> list[dict]:
    try:
        r = requests.get(url, timeout=_TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return []
        articles = _parse_rss(r.text, source=name)
        return [a for a in articles if _is_recent(a["published"])]
    except Exception as exc:
        log.debug(f"fetcher: RSS '{name}' failed — {exc}")
        return []


def _fetch_google_news(query: str) -> list[dict]:
    url = _GN_BASE + query.replace(" ", "+")
    try:
        r = requests.get(url, timeout=_TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return []
        articles = _parse_rss(r.text, source=f"Google News")
        return [a for a in articles if _is_recent(a["published"])]
    except Exception as exc:
        log.debug(f"fetcher: Google News '{query[:40]}' failed — {exc}")
        return []


def _fetch_nse_events() -> list[dict]:
    """
    Fetch upcoming corporate events from NSE India event calendar.
    Requires a session cookie — establishes one by hitting the landing page first.
    """
    session = requests.Session()
    session.headers.update(_NSE_HDRS)
    articles: list[dict] = []
    try:
        session.get(_NSE_MAIN, timeout=15)
        time.sleep(0.8)
        today     = datetime.now()
        look_ahead = today + timedelta(days=14)
        params    = {
            "fromdate": today.strftime("%d-%m-%Y"),
            "todate"  : look_ahead.strftime("%d-%m-%Y"),
        }
        r = session.get(_NSE_EVENTS, params=params, timeout=15)
        if r.status_code != 200:
            return []
        for ev in r.json():
            sym     = ev.get("symbol", "")
            company = ev.get("company", sym)
            purpose = ev.get("purpose", "")
            ev_date = ev.get("date", "")
            if not purpose:
                continue
            articles.append({
                "title"    : f"{company} ({sym}): {purpose} on {ev_date}",
                "summary"  : f"NSE corporate action — {purpose}",
                "link"     : f"https://www.nseindia.com/get-quotes/equity?symbol={sym}",
                "source"   : "NSE Events",
                "published": ev_date,
            })
        log.info(f"fetcher: NSE events — {len(articles)} upcoming actions")
    except Exception as exc:
        log.warning(f"fetcher: NSE events failed — {exc}")
    return articles


# ── Main public API ───────────────────────────────────────────────────────────

def fetch_all_news(max_workers: int = 20) -> list[dict]:
    """
    Fetch news from all sources in parallel.
    Returns a deduplicated list of normalised article dicts, most recent first.

    Each article: {title, summary, link, source, published}
    """
    raw: list[dict] = []

    # Build task list: (func, args)
    tasks: list[tuple] = []
    for name, url in RSS_FEEDS.items():
        tasks.append((_fetch_rss, (name, url)))
    for query in GOOGLE_NEWS_QUERIES:
        tasks.append((_fetch_google_news, (query,)))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fn, *args): fn.__name__ for fn, args in tasks}
        for fut in as_completed(futures):
            try:
                raw.extend(fut.result())
            except Exception as exc:
                log.debug(f"fetcher: task error — {exc}")

    # NSE events (sequential — needs its own session)
    raw.extend(_fetch_nse_events())

    # Dedup by URL, then by (lowercased) title
    seen_urls: set[str]   = set()
    seen_titles: set[str] = set()
    unique: list[dict]    = []
    for art in raw:
        url   = art.get("link",  "").strip()
        title = art.get("title", "").strip().lower()[:80]
        if url and url in seen_urls:
            continue
        if title and title in seen_titles:
            continue
        if url:
            seen_urls.add(url)
        if title:
            seen_titles.add(title)
        unique.append(art)

    # Sort: NSE Events first (highest signal), then by source diversity
    nse_first = [a for a in unique if a.get("source") == "NSE Events"]
    rest      = [a for a in unique if a.get("source") != "NSE Events"]

    result = nse_first + rest
    log.info(
        f"fetcher: {len(raw)} raw articles → {len(result)} unique "
        f"({len(nse_first)} NSE corporate actions)"
    )
    return result
