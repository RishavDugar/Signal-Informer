"""
Web Scout — finds under-the-radar NSE stock picks through three lenses:

  1. Hidden Gems       — contrarian / overlooked catalysts (forums + niche news)
  2. Small-Cap Growth  — emerging companies with strong growth potential
  3. Smart Money       — broking-house ratings + famous-investor portfolio moves
                         (Vijay Kedia, Radhakishan Damani, Ashish Kacholia, etc.)

Each lens runs the same two-pass Ollama flow:
  Pass 1 (think=False): identify candidate symbols in a strict parseable format
  Pass 2 (think=True):  generate a detailed reasoning note per pick

Hidden Gems and Smart Money are restricted to the NSE_100 reference universe
(ScoutConfig.restrict_to_universe=True — the only names with OHLCV history in
our DB). Small-Cap Growth deliberately is NOT (restrict_to_universe=False):
genuine small caps live outside the Nifty 100, so that lens lets the LLM name
any NSE-listed small cap from its own knowledge; technicals for those off-list
picks are sourced via an on-demand yfinance fetch rather than the local DB
(see news_analyzer.pipeline._fetch_recent_ohlcv — fetched data is used only
for the WhatsApp message and is never persisted).

Results are sent as separate WhatsApp messages, one per lens.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests

from config import OLLAMA_GEN_TIMEOUT, OLLAMA_THINK_TIMEOUT
from data.stocks_list import NSE_500
from news_analyzer.ollama_client import generate
from utils.logger import get_logger

log = get_logger("web_scout")

_TIMEOUT   = 12
_MAX_AGE_H = 168   # 7-day window (forum posts / broker notes age slowly)
_GN_BASE   = "https://news.google.com/rss/search?hl=en-IN&gl=IN&ceid=IN:en&q="

# ── Symbol reference (mirrors analyzer.py) ───────────────────────────────────

_SYMBOL_MAP: dict[str, tuple[str, str]] = {
    sym.replace(".NS", "").replace(".BO", ""): (sym, name)
    for sym, name, _ in NSE_500
}
_CLEAN_SYMBOLS = sorted(_SYMBOL_MAP.keys())
_SYMBOL_REF    = "\n".join(
    f"{clean} | {info[1]}"
    for clean, info in sorted(_SYMBOL_MAP.items())
)

_REDDIT_FEEDS: dict[str, str] = {
    "Reddit InvIN"  : "https://www.reddit.com/r/IndiaInvestments/top.rss?t=week",
    "Reddit NSEin"  : "https://www.reddit.com/r/IndianStockMarket/top.rss?t=week",
    "Reddit InvIN2" : "https://www.reddit.com/r/IndiaInvestments/new.rss",
}


# ── Scout lens configuration ──────────────────────────────────────────────────

@dataclass
class ScoutConfig:
    key         : str             # internal id, e.g. "hidden_gems"
    label       : str             # WhatsApp header, e.g. "SCOUT PICKS — Hidden Gems"
    tag         : str             # per-stock tag, e.g. "HIDDEN GEM"
    pick_prefix : str             # Pass-1 line prefix, e.g. "SCOUT" → "SCOUT 1: SYMBOL | ..."
    queries     : list[str]       # Google News RSS search queries
    use_reddit  : bool            # whether to also pull the Reddit feeds
    pass1_prompt: str
    pass2_prompt: str
    # True (default): Pass 1 must pick from the NSE_100 reference list (the only
    # universe with OHLCV history in our DB). False: the lens may name ANY
    # NSE-listed small-cap from its own knowledge — true small caps live well
    # outside the Nifty 100. Off-list picks get their technicals from an
    # on-demand yfinance fetch (see pipeline._fetch_recent_ohlcv) instead of the DB.
    restrict_to_universe: bool = True


# ── Lens 1: Hidden Gems ───────────────────────────────────────────────────────

_HIDDEN_GEMS_QUERIES: list[str] = [
    "NSE midcap smallcap hidden gem stock multibagger 2026",
    "India promoter insider buying stake increase NSE stock",
    "NSE stock 52 week high breakout momentum undervalued 2026",
    "India turnaround story stock recovery earnings surprise",
    "NSE sector rotation underperforming sector catalyst 2026",
    "India deep value stock low PE high ROE NSE 2026",
    "NSE small cap large order win contract announcement",
    "India fund manager stock pick conviction buy 2026",
    "NSE stock analyst initiation coverage buy rating",
    "India export opportunity stock beneficiary 2026",
]

_HIDDEN_GEMS_PASS1 = """\
You are a contrarian Indian equity analyst specialising in finding hidden opportunities \
that mainstream media overlooks.

NSE/BSE listed company reference:
{symbol_ref}

----- SCOUT FEED (forums, niche sources, under-the-radar news) -----
{digest}
-----------------------------------------------------------------------

Your task: Identify the TOP 3 stocks from the NSE/BSE reference list that have \
HIDDEN or OVERLOOKED positive catalysts — NOT the usual RELIANCE/TCS/INFY/HDFC \
headline plays. Look for:
  1. Promoter/insider buying, block deals by smart money
  2. Turnaround stories — improving margins, debt reduction, new management
  3. Sector rotation — sector previously ignored now getting tailwind
  4. Under-the-radar order wins or regulatory approvals
  5. Deep value — low valuation vs peers, high ROE, low PE for growth rate
  6. Forum/community conviction — repeated strong-buy sentiment with reasoning
  7. Export opportunity or import substitution beneficiary

AVOID stocks already prominent in mainstream financial news today.

Respond with EXACTLY this format (nothing else):
SCOUT 1: SYMBOL | Company Name | Hidden catalyst | Why overlooked / contrarian thesis
SCOUT 2: SYMBOL | Company Name | Hidden catalyst | Why overlooked / contrarian thesis
SCOUT 3: SYMBOL | Company Name | Hidden catalyst | Why overlooked / contrarian thesis

Use only symbols from the reference list.
"""

_HIDDEN_GEMS_PASS2 = """\
You are a senior Indian equity analyst writing a contrarian investment note for a \
sophisticated investor who wants hidden opportunities.

Stock: {symbol} — {company}
Hidden catalyst: {catalyst}
Contrarian thesis: {reasoning}

Relevant discussions / news:
{snippets}

Write a 3-sentence investment note explaining:
1. The specific hidden catalyst and why it is not yet priced in
2. The key fundamental reason this stock is undervalued relative to peers
3. The main risk that could invalidate this thesis

Each sentence must be under 30 words. Be specific with numbers where possible. \
Output only the 3 sentences, no headers or labels.
"""

HIDDEN_GEMS = ScoutConfig(
    key          = "hidden_gems",
    label        = "SCOUT PICKS — Hidden Gems",
    tag          = "HIDDEN GEM",
    pick_prefix  = "SCOUT",
    queries      = _HIDDEN_GEMS_QUERIES,
    use_reddit   = True,
    pass1_prompt = _HIDDEN_GEMS_PASS1,
    pass2_prompt = _HIDDEN_GEMS_PASS2,
)


# ── Lens 2: Small-Cap Growth ──────────────────────────────────────────────────

_SMALLCAP_QUERIES: list[str] = [
    "NSE smallcap stock revenue growth margin expansion 2026",
    "India smallcap multibagger stock analyst coverage initiation 2026",
    "smallcap stock India strong quarterly results profit growth surge",
    "NSE smallcap 52 week high breakout volume surge momentum",
    "India emerging smallcap niche market leader growth story 2026",
    "smallcap stock India institutional buying mutual fund accumulation",
    "India smallcap company capacity expansion new product launch growth",
    "NSE smallcap stock re-rating undervalued growth potential 2026",
]

_SMALLCAP_PASS1 = """\
You are a growth-focused Indian equity analyst who specialises in identifying small-cap \
companies before they become widely followed.

----- SMALL-CAP GROWTH FEED -----
{digest}
----------------------------------

Your task: Drawing on the feed above AND your own knowledge of the Indian markets, \
identify the TOP 3 GENUINE SMALL-CAP companies listed on the NSE (typically market \
capitalisation roughly ₹2,000–20,000 crore — NOT large caps or Nifty-100 names such as \
RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK, ITC, LT, SBIN, BHARTIARTL, HINDUNILVR) that show \
the STRONGEST GROWTH POTENTIAL. Prioritise:
  1. High revenue / profit growth (>20% YoY) with margin expansion
  2. Gaining share in a growing niche or emerging sector
  3. Strong order book / pipeline visibility for coming quarters
  4. Recent re-rating triggers — new analyst coverage, institutional buying, capacity expansion
  5. Reasonable valuation relative to its growth rate

For each pick, give its NSE TICKER SYMBOL exactly as traded (e.g. RAINBOW, KAYNES, \
JYOTHYLAB — without any .NS/.BO suffix) and its full company name. Only name companies \
that are genuinely listed on the NSE — never invent a ticker.

Respond with EXACTLY this format (nothing else):
SMALLCAP 1: TICKER | Company Name | Growth driver | One-sentence reasoning
SMALLCAP 2: TICKER | Company Name | Growth driver | One-sentence reasoning
SMALLCAP 3: TICKER | Company Name | Growth driver | One-sentence reasoning
"""

_SMALLCAP_PASS2 = """\
You are a growth-equity analyst writing a concise note on a small-cap growth opportunity.

Stock: {symbol} — {company}
Growth driver: {catalyst}
Initial thesis: {reasoning}

Relevant news / discussion snippets:
{snippets}

Write a 3-sentence growth thesis explaining:
1. The specific growth driver and its likely impact on revenue / earnings over the next 2-4 quarters
2. Why this company can capture the opportunity better than larger / better-known peers
3. The key execution or balance-sheet risk that could derail the growth story

Keep each sentence under 28 words. Output only the 3 sentences, no headers.
"""

SMALL_CAP_GROWTH = ScoutConfig(
    key          = "small_cap_growth",
    label        = "SMALL-CAP PICKS — Growth Potential",
    tag          = "SMALLCAP GROWTH",
    pick_prefix  = "SMALLCAP",
    queries      = _SMALLCAP_QUERIES,
    use_reddit   = False,
    pass1_prompt = _SMALLCAP_PASS1,
    pass2_prompt = _SMALLCAP_PASS2,
    # Real small caps live outside the Nifty 100 — let the LLM name any
    # genuinely NSE-listed small cap rather than constraining to NSE_100.
    restrict_to_universe = False,
)


# ── Lens 3: Smart Money (broking houses + famous investors) ──────────────────

_SMARTMONEY_QUERIES: list[str] = [
    "Vijay Kedia portfolio stock buy stake 2026",
    "Radhakishan Damani RK Damani stock buy stake portfolio 2026",
    "Ashish Kacholia Dolly Khanna Mukul Agrawal stock portfolio buy stake",
    "Motilal Oswal Jefferies Nomura broking house buy rating target price upgrade India",
    "FII DII bulk deal block deal India stock accumulation buying 2026",
    "ace investor India portfolio disclosure stake increase stock 2026",
    "broking house India top stock pick conviction buy recommendation 2026",
    "mutual fund India stock portfolio increase stake buying smallcap midcap",
]

_SMARTMONEY_PASS1 = """\
You are an Indian equity analyst who tracks "smart money" — moves by celebrated individual \
investors and top broking houses.

NSE/BSE listed company reference (clean symbol | company name):
{symbol_ref}

----- SMART MONEY FEED (investor portfolio disclosures, broker ratings, bulk/block deals) -----
{digest}
-----------------------------------------------------------------------------------------------

Your task: From the reference list, identify the TOP 3 stocks showing the STRONGEST \
"smart money" conviction signal. Prioritise, in order:
  1. Disclosed fresh buying / stake increase by well-known investors — e.g. Vijay Kedia, \
Radhakishan Damani, Ashish Kacholia, Dolly Khanna, Mukul Agrawal, Porinju Veliyath, \
Sunil Singhania, Akash Bhansali
  2. Strong BUY / Outperform ratings or target-price upgrades from major broking houses \
(Motilal Oswal, ICICI Securities, Jefferies, Morgan Stanley, Nomura, Kotak Institutional, \
HDFC Securities, JM Financial, Nuvama)
  3. FII / DII bulk deals or block deals showing fresh institutional accumulation
  4. Mutual funds increasing exposure in recent portfolio disclosures

For each pick, NAME the specific investor or broking house in your reasoning.

Respond with EXACTLY this format (nothing else):
SMARTMONEY 1: SYMBOL | Company Name | Smart money signal | One-sentence reasoning naming the investor/broker
SMARTMONEY 2: SYMBOL | Company Name | Smart money signal | One-sentence reasoning naming the investor/broker
SMARTMONEY 3: SYMBOL | Company Name | Smart money signal | One-sentence reasoning naming the investor/broker

Use only symbols from the reference list.
"""

_SMARTMONEY_PASS2 = """\
You are an equity analyst writing a note on a "smart money" conviction stock pick.

Stock: {symbol} — {company}
Smart money signal: {catalyst}
Initial reasoning: {reasoning}

Relevant news / disclosures:
{snippets}

Write a 3-sentence note explaining:
1. WHO is buying or recommending and exactly what they did (stake %, rating, target price, deal size)
2. WHY this signal carries weight — the investor's track record or the broker's conviction level
3. The key risk that could make this smart-money bet wrong

Keep each sentence under 28 words. Output only the 3 sentences, no headers.
"""

SMART_MONEY = ScoutConfig(
    key          = "smart_money",
    label        = "SMART MONEY PICKS — Investor & Broker Signals",
    tag          = "SMART MONEY",
    pick_prefix  = "SMARTMONEY",
    queries      = _SMARTMONEY_QUERIES,
    use_reddit   = False,
    pass1_prompt = _SMARTMONEY_PASS1,
    pass2_prompt = _SMARTMONEY_PASS2,
)


ALL_SCOUTS: list[ScoutConfig] = [HIDDEN_GEMS, SMALL_CAP_GROWTH, SMART_MONEY]


# ── Fetching (shared) ─────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_recent(pub_str: str) -> bool:
    if not pub_str:
        return True
    try:
        dt = parsedate_to_datetime(pub_str).astimezone(timezone.utc)
    except Exception:
        return True
    return dt >= datetime.now(timezone.utc) - timedelta(hours=_MAX_AGE_H)


def _parse_rss(content: str, source: str) -> list[dict]:
    articles: list[dict] = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return articles
    ns = "http://www.w3.org/2005/Atom"
    for item in root.iter("item"):
        title   = _strip_html(item.findtext("title",       ""))
        summary = _strip_html(item.findtext("description", ""))[:300]
        link    = (item.findtext("link") or "").strip()
        pub     = (item.findtext("pubDate") or "").strip()
        if title:
            articles.append({"title": title, "summary": summary,
                              "link": link, "source": source, "published": pub})
    for entry in root.iter(f"{{{ns}}}entry"):
        title   = _strip_html(entry.findtext(f"{{{ns}}}title",   ""))
        summary = _strip_html(entry.findtext(f"{{{ns}}}summary", ""))[:300]
        el      = entry.find(f"{{{ns}}}link")
        link    = (el.get("href", "") if el is not None else "").strip()
        pub     = (entry.findtext(f"{{{ns}}}updated") or
                   entry.findtext(f"{{{ns}}}published") or "").strip()
        if title:
            articles.append({"title": title, "summary": summary,
                              "link": link, "source": source, "published": pub})
    return articles


def _fetch_rss(name: str, url: str) -> list[dict]:
    try:
        r = requests.get(url, timeout=_TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0 (compatible)"})
        if r.status_code != 200:
            log.debug(f"scout: {name} returned HTTP {r.status_code}")
            return []
        arts = _parse_rss(r.text, name)
        recent = [a for a in arts if _is_recent(a["published"])]
        log.debug(f"scout: {name} — {len(recent)} recent articles")
        return recent
    except Exception as exc:
        log.debug(f"scout: {name} failed — {exc}")
        return []


def _fetch_for_config(config: ScoutConfig, max_workers: int = 12) -> list[dict]:
    """Fetch articles for a scout lens: optional Reddit feeds + its Google News queries."""
    tasks: list[tuple] = []
    if config.use_reddit:
        for name, url in _REDDIT_FEEDS.items():
            tasks.append((_fetch_rss, (name, url)))
    for query in config.queries:
        url = _GN_BASE + query.replace(" ", "+")
        tasks.append((_fetch_rss, ("GN Scout", url)))

    raw: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(fn, *args) for fn, args in tasks]
        for fut in as_completed(futures):
            try:
                raw.extend(fut.result())
            except Exception:
                pass

    seen: set[str] = set()
    unique: list[dict] = []
    for art in raw:
        key = art.get("title", "").lower()[:80]
        if key and key not in seen:
            seen.add(key)
            unique.append(art)

    log.info(f"scout[{config.key}]: {len(raw)} raw → {len(unique)} unique articles "
             f"from {len(tasks)} sources")
    return unique


def _build_digest(articles: list[dict], max_articles: int = 60) -> str:
    reddit = [a for a in articles if "Reddit" in a.get("source", "")]
    other  = [a for a in articles if "Reddit" not in a.get("source", "")]
    pool   = (reddit + other)[:max_articles]

    lines = []
    for i, art in enumerate(pool, 1):
        src   = art.get("source", "")
        title = art.get("title", "").strip()
        summ  = art.get("summary", "").strip()[:80]
        line  = f"{i}. [{src}] {title}"
        if summ and summ.lower() not in title.lower():
            line += f" — {summ}"
        lines.append(line)
    return "\n".join(lines)


# ── Parsing (shared, parameterised by line prefix) ───────────────────────────

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9&\-]{0,14}$")


def _parse_picks(response: str, prefix: str, restrict_to_universe: bool = True) -> list[dict]:
    """
    Parse "PREFIX N: SYMBOL | Company | Catalyst | Reasoning" lines.

    restrict_to_universe=True  — symbol must resolve to an NSE_100 reference entry
                                 (fuzzy-matched if not an exact clean-symbol hit).
    restrict_to_universe=False — any plausible NSE ticker is accepted as-is
                                 (full symbol assembled as TICKER.NS); used for
                                 lenses like Small-Cap Growth whose universe is
                                 intentionally NOT limited to the Nifty 100.
    """
    picks: list[dict] = []
    seen: set[str] = set()
    for raw_line in response.splitlines():
        line = re.sub(r"^[#*>\-_=|`\s]+", "", raw_line.strip()).strip()
        if not line or "|" not in line:
            continue
        m = re.match(rf"(?:{prefix}\s*\d+\s*:?\s*|\d+[.):\s]+)\s*(.+)", line, re.I)
        content = m.group(1) if m else line
        parts   = [p.strip() for p in content.split("|")]
        if not parts or parts[0].lower() in {prefix.lower(), "symbol", "ticker", "rank", "#", ""}:
            continue
        raw_sym  = re.sub(r"\.(NS|BO)$", "", parts[0].upper().strip()).rstrip(".,:;")
        company  = parts[1].strip() if len(parts) > 1 else ""
        catalyst = parts[2].strip() if len(parts) > 2 else ""
        reasoning= parts[3].strip() if len(parts) > 3 else ""

        clean = raw_sym
        if clean in _SYMBOL_MAP:
            full_sym, full_name = _SYMBOL_MAP[clean]
            company = full_name or company
        elif restrict_to_universe:
            matches = [s for s in _CLEAN_SYMBOLS if clean in s or s in clean]
            if not matches:
                log.debug(f"scout: unknown symbol '{raw_sym}' — skipping")
                continue
            clean = matches[0]
            full_sym, full_name = _SYMBOL_MAP[clean]
            company = full_name or company
        else:
            # Off-list pick (e.g. genuine small cap outside the Nifty 100):
            # trust the LLM's ticker + name, but sanity-check it looks like a
            # real NSE ticker and that a company name was actually given.
            if not _TICKER_RE.match(clean) or not company:
                log.debug(f"scout: '{raw_sym}' doesn't look like a valid off-list pick — skipping")
                continue
            full_sym = f"{clean}.NS"

        if clean in seen:
            continue
        seen.add(clean)
        picks.append({
            "symbol"   : full_sym,
            "clean"    : clean,
            "company"  : company,
            "catalyst" : catalyst,
            "reasoning": reasoning,
            "is_scout" : True,
        })
    return picks


def _relevant_snippets(clean: str, company: str, articles: list[dict],
                       max_snips: int = 5) -> str:
    kws = {clean.lower(), company.lower().split()[0]}
    snips = []
    for art in articles:
        text = (art.get("title", "") + " " + art.get("summary", "")).lower()
        if any(kw in text for kw in kws):
            snips.append(f"- [{art.get('source','')}] {art['title']}")
            if len(snips) >= max_snips:
                break
    return "\n".join(snips) if snips else "- No direct mentions found; based on sector / macro context."


# ── Public API ────────────────────────────────────────────────────────────────

def run_scout(config: ScoutConfig, top_n: int = 3,
              exclude_symbols: set[str] | None = None) -> list[dict]:
    """
    Run one scout lens end-to-end: fetch → Pass 1 (pick symbols) → Pass 2 (thesis).
    Returns a list of pick dicts with 'analysis', 'scout_type', 'is_scout': True.

    Pass 1 uses think=False (structured output reliability — see ollama_client.generate).
    Pass 2 uses think=True (free-form reasoning benefits from chain-of-thought).

    exclude_symbols: full symbols (e.g. "TCS.NS") this lens already surfaced
    within the SCOUT_DEDUP_DAYS window (see news_analyzer.db.get_recently_scouted).
    Passed to Pass 1 as explicit negative context so the model spends its pick
    budget hunting for genuinely NEW names instead of re-analysing — and us
    later discarding — repeats. The pipeline still re-filters post-hoc as a
    backstop in case the model repeats one anyway.
    """
    articles = _fetch_for_config(config)
    if not articles:
        log.warning(f"scout[{config.key}]: no articles fetched — skipping")
        return []

    digest = _build_digest(articles)

    log.info(f"scout[{config.key}]: Pass 1 — digest={len(digest)} chars, "
             f"{len(articles)} articles")
    if config.restrict_to_universe:
        prompt1 = config.pass1_prompt.format(symbol_ref=_SYMBOL_REF, digest=digest)
    else:
        prompt1 = config.pass1_prompt.format(digest=digest)

    if exclude_symbols:
        avoid_clean = sorted({re.sub(r"\.(NS|BO)$", "", s.upper()) for s in exclude_symbols})
        prompt1 += (
            "\n\nIMPORTANT — these were already surfaced by this exact scan within "
            "the last few days; they have already been sent out, so DO NOT pick "
            "them again — actively look for DIFFERENT names instead:\n"
            + ", ".join(avoid_clean) + "\n"
        )
        log.info(f"scout[{config.key}]: Pass 1 — excluding {len(avoid_clean)} "
                 f"recently-scouted symbol(s) from consideration")

    log.info(f"scout[{config.key}]: Pass 1 prompt = {len(prompt1)} chars "
             f"(~{len(prompt1)//4} tokens est.)")
    try:
        response = generate(prompt1, timeout=OLLAMA_GEN_TIMEOUT, think=False)
        log.info(f"scout[{config.key}]: Pass 1 raw response (first 400 chars):\n{response[:400]}")
    except Exception as exc:
        log.warning(f"scout[{config.key}]: Pass 1 failed — {exc}")
        return []

    picks = _parse_picks(response, config.pick_prefix, restrict_to_universe=config.restrict_to_universe)
    if not picks:
        log.warning(f"scout[{config.key}]: Pass 1 returned no parseable picks")
        log.warning(f"scout[{config.key}]: raw response (first 800 chars):\n{response[:800]}")
        return []

    # Backstop: drop any repeat that slipped through despite the avoid-list
    # context, BEFORE spending a Pass-2 (think=True) call analysing it.
    if exclude_symbols:
        before = len(picks)
        picks = [p for p in picks if p["symbol"] not in exclude_symbols]
        if len(picks) != before:
            log.info(f"scout[{config.key}]: dropped {before - len(picks)} repeat "
                     f"pick(s) the model surfaced despite the avoid-list")
        if not picks:
            log.warning(f"scout[{config.key}]: all Pass 1 picks were repeats — nothing new this run")
            return []

    picks = picks[:top_n]
    for p in picks:
        p["scout_type"] = config.key
    log.info(f"scout[{config.key}]: {len(picks)} pick(s): {[p['clean'] for p in picks]}")

    for pick in picks:
        snips   = _relevant_snippets(pick["clean"], pick["company"], articles)
        prompt2 = config.pass2_prompt.format(
            symbol   = pick["clean"],
            company  = pick["company"],
            catalyst = pick["catalyst"],
            reasoning= pick["reasoning"],
            snippets = snips,
        )
        log.info(f"scout[{config.key}]: Pass 2 — {pick['clean']}")
        # think=True can occasionally spiral into a very long hidden "thinking"
        # trace (probabilistic, not bounded by num_predict — see the calibration
        # notes + timeout=210s/num_predict=3072 rationale in news_analyzer.analyzer's
        # Pass 2) and either return empty or blow past the timeout. Either failure
        # falls through to a think=False retry, which skips the thinking step and
        # answers directly in ~10-25s, so a pick never ships with blank analysis text.
        analysis = ""
        try:
            analysis = generate(prompt2, timeout=OLLAMA_THINK_TIMEOUT, think=True)
            if not analysis:
                log.warning(f"scout[{config.key}]: Pass 2 think=True returned empty "
                            f"for {pick['clean']} — retrying with think=False")
        except Exception as exc:
            log.warning(f"scout[{config.key}]: Pass 2 think=True failed for "
                        f"{pick['clean']} — {exc} — retrying with think=False")

        if not analysis:
            try:
                analysis = generate(prompt2, timeout=OLLAMA_GEN_TIMEOUT, think=False)
            except Exception as exc:
                log.warning(f"scout[{config.key}]: Pass 2 think=False fallback "
                            f"also failed for {pick['clean']} — {exc}")
        pick["analysis"] = analysis or pick["reasoning"]

    return picks
