"""
On-demand single-stock research.

Triggered by a "<trigger> <SYMBOL>" WhatsApp message (e.g. "Search RELIANCE.NS")
dropped into the research group — see research_listener.py for the watcher that
calls research_symbol() and replies in the group.

Two independent halves, each degrading to its own error line so a partial result
still ships:

  1. Price + technicals — local OHLCV DB first, then an on-demand yfinance fetch,
     run through the SAME enrichment the news pipeline uses (CMP, 1D/5D/20D moves,
     RSI(14), volume ratio, 20-SMA trend). If BOTH sources come up empty, the
     block becomes "failed to gather stock price data".
  2. Web-news insights — a focused Google-News search for the stock, condensed by
     the local Ollama model into a short thesis. If no articles are found, Ollama
     is unavailable, or it returns nothing, the block becomes a "no insights" note.

The two blocks are concatenated and returned as one WhatsApp-ready string.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests

from config import OLLAMA_GEN_TIMEOUT, OLLAMA_THINK_TIMEOUT
from news_analyzer.analyzer import _CLEAN_SYMBOLS, _SYMBOL_MAP
from news_analyzer.fetcher import (
    RSS_FEEDS, _NSE_HDRS, _NSE_MAIN, _fetch_google_news, _fetch_rss,
)
from news_analyzer.formatter import _price_line, _technical_line
from news_analyzer.web_scout import _REDDIT_FEEDS
from news_analyzer.ollama_client import (
    available_model, ensure_available, generate, warmup_model,
)
from news_analyzer.pipeline import _enrich_technicals
from utils.logger import get_logger

log = get_logger("research")

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9&\-]{0,14}$")

# ── "Garbage" headline detection ──────────────────────────────────────────────
# Two tiers, deliberately different in severity:
#  • NOISE — non-articles (live quote pages, video clips). Useless even as context,
#    so dropped before the LLM ever sees them.
#  • ROUNDUP — sector/index/multi-stock pieces that only name the stock among many.
#    KEPT as context for the briefing (a sector move is real, useful background) but
#    never shown as a "headline" for this stock (that's what looked like garbage).
_WORD       = re.compile(r"[a-z0-9]+")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")          # Hindi market-live clips
_YT_ID      = re.compile(r"\([0-9A-Za-z_\-]{9,}\)")   # trailing YouTube video id
_NOISE_PHRASES = (
    "live updates", "share price live", "share price today", "stock price for nse",
    "market live", "heatmap", "watchlist", "muhurat", "live blog",
)
_ROUNDUP_PHRASES = (
    "in focus", "stocks to watch", "stocks to buy", "stocks in news",
    "stocks in focus", "top stocks", "top gainers", "top losers", "buzzing stocks",
    "what to do", "these stocks", "best stocks", "multibagger stocks", "stocks to add",
    "it stocks", "nifty it", "it index", "sectoral indices", "sectoral index",
    "drag nifty", "drag sensex", "bank nifty", "metal stocks", "auto stocks",
    "pharma stocks", "fmcg stocks", "psu stocks", "realty stocks", "sensex today",
    "nifty today", "among sectoral", "top movers", "stocks in spotlight",
)
# Generic words that appear in many company names — excluded so they don't count
# as "another company" when detecting roundups.
_NAME_STOP = {
    "share", "stock", "price", "market", "live", "today", "news", "limited",
    "india", "indian", "industries", "bank", "steel", "power", "motors",
    "finance", "financial", "group", "company", "corporation", "sensex", "nifty",
    "update", "updates", "life", "insurance", "ports", "energy", "auto",
}
_NAME_TOKENS: set[str] = set()
for _c, (_f, _n) in _SYMBOL_MAP.items():
    if len(_c) >= 3:
        _NAME_TOKENS.add(_c.lower())
    for _w in _WORD.findall((_n or "").lower()):
        if len(_w) >= 5 and _w not in _NAME_STOP:
            _NAME_TOKENS.add(_w)


def _own_tokens(sym: dict) -> set[str]:
    """The stock's own name tokens (ticker, company words, and any user keywords) —
    so they're never counted as 'another company' when detecting roundups."""
    toks = {sym["clean"].lower()}
    names = (sym.get("company") or "") + " " + " ".join(sym.get("keywords") or [])
    for w in _WORD.findall(names.lower()):
        if len(w) >= 4 and w not in _NAME_STOP:
            toks.add(w)
    return toks


def _news_terms(sym: dict) -> tuple[str, str]:
    """(primary, extra) search terms for the news fetch. With user keywords the
    first keyword is the primary entity (e.g. 'Balmer Lawrie' for ticker BLIL,
    whose news never says 'BLIL') and the rest add context; otherwise fall back to
    the resolved company name, then the ticker."""
    kws = [k for k in (sym.get("keywords") or []) if k]
    if kws:
        return kws[0], " ".join(kws[1:])
    return (sym.get("company") or sym["clean"]), ""


def _is_noise(art: dict) -> bool:
    """Non-article junk (live quote pages, video clips) — useless even as context."""
    title = art.get("title", "")
    if _DEVANAGARI.search(title) or _YT_ID.search(title):
        return True
    return any(p in title.lower() for p in _NOISE_PHRASES)


def _is_roundup(art: dict, own: set[str]) -> bool:
    """Sector/index/multi-stock piece that only names the stock among many — fine as
    briefing context, but not a stock-specific headline."""
    low = art.get("title", "").lower()
    if any(p in low for p in _ROUNDUP_PHRASES):
        return True
    # Names two or more OTHER known companies → a multi-stock roundup.
    others = (set(_WORD.findall(low)) & _NAME_TOKENS) - own
    return len(others) >= 2


# ── Symbol resolution ─────────────────────────────────────────────────────────

def resolve_symbol(raw: str) -> dict | None:
    """
    Turn a user token ("RELIANCE", "reliance.ns", "TATAMOTORS.BO") into
    {symbol, clean, company}, or None if it doesn't look like a tradable ticker.

    Exact NSE_500 match wins; otherwise a loose substring match against the known
    universe; otherwise the token is accepted as an off-list NSE ticker (.NS) so
    names outside our DB can still be researched via the yfinance fallback.
    """
    clean = re.sub(r"\.(NS|BO)$", "", (raw or "").strip().upper()).rstrip(".,:;?!")
    if not clean:
        return None
    if clean in _SYMBOL_MAP:
        full, name = _SYMBOL_MAP[clean]
        return {"symbol": full, "clean": clean, "company": name}
    matches = [s for s in _CLEAN_SYMBOLS if clean == s or clean in s or s in clean]
    if matches:
        best = min(matches, key=len)
        full, name = _SYMBOL_MAP[best]
        return {"symbol": full, "clean": best, "company": name}
    if _TICKER_RE.match(clean):
        return {"symbol": f"{clean}.NS", "clean": clean, "company": ""}
    return None


# ── 1. Price + technicals ─────────────────────────────────────────────────────

def _price_block(sym: dict) -> str | None:
    """Price + technical lines for the stock, or None if no price data could be
    gathered from either the local DB or yfinance."""
    rec = _enrich_technicals({"symbol": sym["symbol"]})
    if rec.get("cmp") is None:
        return None
    lines = [ln for ln in (_price_line(rec), _technical_line(rec)) if ln]
    return "\n".join(lines) if lines else None


# ── 2. Web-news insights ──────────────────────────────────────────────────────

_INSIGHT_PROMPT = """\
You are a senior Indian equity analyst. Summarise the latest news for an investor.

Stock: {symbol} — {company}

Numbered news items (financial press, broker notes, and investor-forum chatter):
{snippets}

Write a concise 3-4 sentence briefing covering:
1. The most important recent development(s) for this stock
2. Any SMART-MONEY signal — promoter/insider buying, bulk/block deals, FII/DII
   flows, broking-house ratings or target-price changes, or marquee-investor
   activity — if the items show one
3. What it means for the share price (direction and why) and the key risk to watch

Use only what the items support; forum/Reddit chatter is sentiment, not fact. If
the news is thin or mixed, say so.

Then, on a FINAL separate line, list the item numbers that are SPECIFICALLY about
{company} as the primary subject — EXCLUDE sector/index roundups and items that
merely mention it among several stocks — in exactly this format:
SOURCES: 1, 3
Output only the briefing followed by that single SOURCES line.
"""

# Keywords that mark a snippet as carrying a smart-money signal — insider/promoter
# activity, institutional flows, broking-house coverage, or marquee investors —
# used to push those items to the front so the briefing/headlines lead with them.
_INSIDER_KW = (
    # insider / institutional
    "promoter", "insider", "bulk deal", "block deal", "stake", "fii", "dii",
    "sast", "buyback", "pledge", "acquire", "open offer", "institutional",
    # broking houses / research / consultancies
    "brokerage", "broker", "target price", "rating", "upgrade", "downgrade",
    "initiate", "coverage", "outperform", "overweight", "buy call", "motilal",
    "jefferies", "nomura", "morgan stanley", "kotak", "nuvama", "clsa", "ubs",
    "goldman", "macquarie", "citi", "icici securities", "hdfc securities",
    "jm financial", "emkay", "antique", "investec",
    # marquee / famous investors
    "kedia", "damani", "kacholia", "jhunjhunwala", "dolly khanna", "mukul agrawal",
    "porinju", "singhania", "bhansali", "ace investor", "superstar investor",
)


def _nse_recent(dt_str: str, cutoff: datetime) -> bool:
    """True if an NSE date string (e.g. '18-Jun-2026 15:53:52' / '18-Jun-2026') is
    on/after cutoff; unknown formats are kept (treated as recent)."""
    s = (dt_str or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt) >= cutoff
        except ValueError:
            continue
    return True


def _fetch_nse_symbol_items(sym: dict) -> list[dict]:
    """
    Hidden, per-symbol DIRECT data straight from NSE (not news aggregators) — the
    actual record of where money is moving in this stock:
      • Bulk / block / short deals (client name, buy/sell, qty, avg price)
      • Recent corporate announcements (orders, board outcomes, disclosures)
    Needs the NSE session-cookie dance (hit the landing page first). Best-effort:
    returns [] on any hiccup. These items are treated as always-relevant.
    """
    clean = sym["clean"].upper()
    items: list[dict] = []
    try:
        s = requests.Session()
        s.headers.update(_NSE_HDRS)
        s.get(_NSE_MAIN, timeout=12)
        time.sleep(0.6)

        # Bulk / block / short deals — filter the market-wide snapshot to this symbol.
        try:
            r = s.get(f"{_NSE_MAIN}/api/snapshot-capital-market-largedeal", timeout=12)
            j = r.json() if r.ok else {}
            for key, label in (("BULK_DEALS_DATA", "BULK DEAL"),
                               ("BLOCK_DEALS_DATA", "BLOCK DEAL"),
                               ("SHORT_DEALS_DATA", "SHORT SELL")):
                for d in (j.get(key) or []):
                    if str(d.get("symbol", "")).upper() != clean:
                        continue
                    items.append({
                        "title": (f"{label}: {d.get('clientName', '?')} "
                                  f"{d.get('buySell', '')} {d.get('qty', '')} @ "
                                  f"Rs{d.get('watp', '')} ({d.get('date', '')})"),
                        "summary": "", "link": "",
                        "source": f"NSE {label}", "published": d.get("date", ""),
                    })
        except Exception as exc:
            log.debug(f"research[{clean}]: NSE deals fetch failed — {exc}")

        # Recent corporate announcements for this symbol (last ~14 days).
        try:
            r = s.get(f"{_NSE_MAIN}/api/corporate-announcements",
                      params={"index": "equities", "symbol": clean}, timeout=12)
            rows = r.json() if r.ok else []
            if isinstance(rows, dict):
                rows = rows.get("data", [])
            cutoff = datetime.now() - timedelta(days=14)
            n = 0
            for a in rows:
                desc = (a.get("desc") or a.get("attchmntText") or "").strip()
                dt   = a.get("an_dt") or a.get("sort_date") or ""
                if not desc or not _nse_recent(dt, cutoff):
                    continue
                items.append({
                    "title": f"{clean}: {desc}"[:200],
                    "summary": (a.get("attchmntText") or "")[:200],
                    "link": a.get("attchmntFile", ""),
                    "source": "NSE Announcement", "published": dt,
                })
                n += 1
                if n >= 6:
                    break
        except Exception as exc:
            log.debug(f"research[{clean}]: NSE announcements fetch failed — {exc}")
    except Exception as exc:
        log.debug(f"research[{clean}]: NSE session failed — {exc}")

    if items:
        log.info(f"research[{clean}]: NSE direct data — {len(items)} deal/announcement item(s)")
    return items


def _fetch_symbol_news(sym: dict, max_workers: int = 16) -> list[dict]:
    """
    Pull stock news from several source groups, in parallel, de-duped by headline:
      • NSE direct data — actual bulk/block deals + corporate announcements (hidden,
        per-symbol; captures real money moves mainstream feeds miss)
      • Google News — general + insider/smart-money/broker/investor queries
      • Reddit — Indian investing forums (insider chatter / contrarian sentiment)
      • Curated financial RSS — ET, MoneyControl, Mint, Business Standard, etc.
    News searches key off `primary` (user keyword / company name) so small-caps whose
    news never uses the ticker (e.g. 'Balmer Lawrie' vs BLIL) are still covered.
    """
    clean = sym["clean"]
    primary, extra = _news_terms(sym)
    gn_queries = [
        f"{primary} share price NSE {extra}".strip(),
        f"{primary} stock news India",
        f"{primary} quarterly results order win deal",
        f"{primary} promoter insider stake buying purchase",
        f"{primary} bulk deal block deal FII DII institutional",
        f"{primary} SAST insider trading shareholding buyback disclosure",
        # broking houses + research/consultancy coverage
        f"{primary} brokerage target price rating upgrade Motilal Oswal Jefferies Nomura Kotak",
        f"{primary} analyst initiate coverage buy outperform research report",
        # marquee / famous investors
        f"{primary} Vijay Kedia Radhakishan Damani Ashish Kacholia Mukul Agrawal portfolio stake",
        f"{primary} ace investor superstar investor holding stake increase",
    ]
    tasks: list[tuple] = [(_fetch_google_news, (q,)) for q in gn_queries]
    tasks += [(_fetch_rss, (name, url)) for name, url in _REDDIT_FEEDS.items()]
    tasks += [(_fetch_rss, (name, url)) for name, url in RSS_FEEDS.items()]

    # NSE direct data first (highest-signal, always relevant), then the feeds.
    articles: list[dict] = list(_fetch_nse_symbol_items(sym))
    seen: set[str] = {a["title"].lower()[:80] for a in articles}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(fn, *args) for fn, args in tasks]
        for fut in as_completed(futs):
            try:
                for art in fut.result():
                    key = art.get("title", "").lower()[:80]
                    if key and key not in seen:
                        seen.add(key)
                        articles.append(art)
            except Exception:
                pass
    log.info(f"research[{clean}]: fetched {len(articles)} unique article(s) "
             f"from {len(tasks)} feeds + NSE direct data")
    return articles


def _relevant_articles(sym: dict, articles: list[dict]) -> list[dict]:
    """Articles that mention this stock — by ticker, company first word, or any user
    keyword (so 'Balmer Lawrie' news matches ticker BLIL) — and aren't pure noise.
    NSE direct data (per-symbol) is always kept. Sector/roundup items ARE kept as
    briefing context; headline selection is what excludes them from what's shown."""
    # Relevance keys off the ticker, the company's first word, and the PRIMARY
    # keyword (the company name) only — not the secondary keywords, which are often
    # generic ('PSU', 'Investments') and would pull in unrelated articles. The
    # secondaries still shape the search queries and roundup detection.
    kws = {sym["clean"].lower()}
    company = (sym["company"] or "").lower().split()
    if company:
        kws.add(company[0])
    primary, _ = _news_terms(sym)
    if primary:
        kws.add(primary.lower())
    kws.discard("")
    out: list[dict] = []
    for art in articles:
        if art.get("source", "").startswith("NSE "):   # per-symbol direct data
            out.append(art)
            continue
        text = (art.get("title", "") + " " + art.get("summary", "")).lower()
        if any(kw in text for kw in kws) and not _is_noise(art):
            out.append(art)

    # Lead with NSE direct data + insider/smart-money items, then forum chatter.
    def _rank(art: dict) -> int:
        if art.get("source", "").startswith("NSE "):
            return -1
        text = (art.get("title", "") + " " + art.get("summary", "")).lower()
        if any(kw in text for kw in _INSIDER_KW):
            return 0
        if "reddit" in art.get("source", "").lower():
            return 1
        return 2

    out.sort(key=_rank)
    return out


def _news_block(sym: dict) -> str | None:
    """A short LLM-written news briefing for the stock, or None if no stock-
    specific articles were found, Ollama is unavailable, or it produced nothing."""
    if not ensure_available():
        log.warning(f"research[{sym['clean']}]: Ollama unavailable — no insights")
        return None

    relevant = _relevant_articles(sym, _fetch_symbol_news(sym))
    if not relevant:
        log.info(f"research[{sym['clean']}]: no stock-specific articles — no insights")
        return None

    # Number the candidates so the model can point back at the ones genuinely
    # about this stock (it sees, and selects from, these exact items).
    cands = relevant[:10]
    snippets = "\n".join(f"{i}. [{a.get('source','')}] {a['title']}"
                         for i, a in enumerate(cands, 1) if a.get("title"))
    primary, _ = _news_terms(sym)
    prompt = _INSIGHT_PROMPT.format(
        symbol=sym["clean"], company=sym["company"] or primary, snippets=snippets)

    warmup_model()
    analysis = ""
    try:
        analysis = generate(prompt, timeout=OLLAMA_THINK_TIMEOUT, think=True)
    except Exception as exc:
        log.warning(f"research[{sym['clean']}]: insight think=True failed — {exc}")
    if not analysis:
        try:
            analysis = generate(prompt, timeout=OLLAMA_GEN_TIMEOUT, think=False)
        except Exception as exc:
            log.warning(f"research[{sym['clean']}]: insight think=False failed — {exc}")
    if not analysis:
        return None

    # The model ends with "SOURCES: n, n" naming the items truly about this stock.
    # Prefer those as the shown headlines (LLM-refined); strip the line from prose.
    own = _own_tokens(sym)
    chosen: list[dict] = []
    m = re.search(r"(?im)^\s*sources?\s*[:\-]\s*(.+)$", analysis)
    if m:
        idx = [int(n) for n in re.findall(r"\d+", m.group(1))]
        chosen = [cands[i - 1] for i in idx if 1 <= i <= len(cands)]
        analysis = analysis[:m.start()].strip()
    # Guard / fallback: never show a roundup as a stock-specific headline.
    chosen = [a for a in chosen if not _is_roundup(a, own)]
    if not chosen:
        chosen = [a for a in cands if not _is_roundup(a, own)]

    block = analysis.strip()
    heads = [f"• {a['title']}" for a in chosen[:3] if a.get("title")]
    if heads:
        block += "\n\n_Recent headlines:_\n" + "\n".join(heads)
    else:
        # Briefing stands on its own (sector/general context); be explicit that
        # there was no stock-SPECIFIC news to headline, rather than omitting it.
        block += f"\n❌ No recent news insights found specific for {sym['clean']}."
    return block


# ── Public entry point ────────────────────────────────────────────────────────

def research_symbol(query: str, keywords: list[str] | None = None) -> str:
    """
    Research one stock end-to-end and return a single WhatsApp-ready message.

    `query`    — the ticker (drives price/technicals via the local DB / yfinance).
    `keywords` — optional news search terms from the request's "Keywords:" line; the
                 first is the primary entity (company name) used for the news search,
                 so a ticker whose news never uses it (BLIL → 'Balmer Lawrie') is
                 still covered. Without keywords, the resolved company name is used.

    Always returns a string — the price and news halves degrade to their own error
    lines independently, so a partial result still gets delivered.
    """
    sym = resolve_symbol(query)
    if not sym:
        return (f"*Stock Research*\n"
                f"❌ Could not recognise a stock symbol in \"{query.strip()}\".\n"
                f"_Try e.g. *{__example_trigger()} RELIANCE.NS*_")
    sym["keywords"] = [k.strip() for k in (keywords or []) if k.strip()]

    display = sym["clean"]
    log.info(f"research: START  query={query!r}  symbol={sym['symbol']}  "
             f"keywords={sym['keywords']}")

    price_block = _price_block(sym)
    news_block  = _news_block(sym)

    header = [f"*Stock Research — {display}*"]
    name = sym["company"] or (sym["keywords"][0] if sym["keywords"] else "")
    if name:
        header.append(name)

    body: list[str] = []
    if price_block:
        body += ["", price_block]
    else:
        body += ["", f"❌ Failed to gather stock price data for {display}."]

    body += ["", "*News & Insights*"]
    if news_block:
        body.append(news_block)
    else:
        body.append(f"❌ No recent news insights found for {display}.")

    body += ["", "_On-demand research · price/technicals + Ollama AI news read. "
                 "Not investment advice._"]

    log.info(f"research: DONE  symbol={sym['symbol']}  "
             f"price={'ok' if price_block else 'FAIL'}  "
             f"news={'ok' if news_block else 'FAIL'}")
    return "\n".join(header + body).strip()


def __example_trigger() -> str:
    from config import RESEARCH_TRIGGER
    return RESEARCH_TRIGGER.capitalize()
