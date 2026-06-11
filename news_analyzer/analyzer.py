"""
News-to-stock-pick analyzer powered by a local Ollama model.

Two-pass strategy
-----------------
Pass 1  (fast, ~20s): Send all article headlines to Ollama.
        Ask it to identify which NSE 100 stocks have the strongest
        positive catalyst.  Response is a ranked list of symbols.

Pass 2  (deep, ~60s): For each shortlisted symbol, send the
        relevant article snippets and ask for a detailed 3-sentence
        analysis explaining the investment thesis.

The NSE 100 symbol list is embedded in every prompt so the model
returns exact ticker symbols instead of free-text company names.
"""

from __future__ import annotations

import json
import re
from typing import Any

from data.stocks_list import NSE_500
from news_analyzer.ollama_client import generate
from utils.logger import get_logger

log = get_logger("news_analyzer")

# ── Symbol reference table ────────────────────────────────────────────────────

# Maps clean symbol (no .NS/.BO) → full yfinance symbol + display name
_SYMBOL_MAP: dict[str, tuple[str, str]] = {
    sym.replace(".NS", "").replace(".BO", ""): (sym, name)
    for sym, name, _ in NSE_500
}
_CLEAN_SYMBOLS = sorted(_SYMBOL_MAP.keys())

# Inline reference block for prompts (symbol | company)
_SYMBOL_REF = "\n".join(
    f"{clean} | {info[1]}"
    for clean, info in sorted(_SYMBOL_MAP.items())
)

# ── Relevance filtering ───────────────────────────────────────────────────────

_INDIA_KEYWORDS = {
    "nse", "bse", "nifty", "sensex", "sebi", "rbi", "india", "indian",
    "rupee", "lakh", "crore", "ipo", "fii", "dii", "promoter",
    "bonus", "dividend", "buyback", "demerger", "merger", "acquisition",
    "quarterly", "results", "earnings", "guidance", "order", "contract",
    "approval", "launch", "stake",
}


def _is_india_relevant(article: dict) -> bool:
    text = (article.get("title", "") + " " + article.get("summary", "")).lower()
    return any(kw in text for kw in _INDIA_KEYWORDS)


def filter_relevant(articles: list[dict]) -> list[dict]:
    """Keep only India/market-relevant articles; cap at 120."""
    relevant = [a for a in articles if _is_india_relevant(a)]
    log.info(f"analyzer: {len(relevant)}/{len(articles)} articles pass relevance filter")
    return relevant[:120]


# ── Digest builder ────────────────────────────────────────────────────────────

def build_digest(articles: list[dict], max_articles: int = 80) -> str:
    """
    Compact one-liner per article: [SOURCE] TITLE — first 100 chars of summary.
    NSE Events (highest signal) are always included first.
    """
    nse   = [a for a in articles if a.get("source") == "NSE Events"]
    other = [a for a in articles if a.get("source") != "NSE Events"]

    # Interleave: keep all NSE events + fill to max_articles from other sources
    pool = nse + other
    pool = pool[:max_articles]

    lines = []
    for i, art in enumerate(pool, 1):
        src   = art.get("source", "")
        title = art.get("title", "").strip()
        summ  = art.get("summary", "").strip()[:100]
        line  = f"{i}. [{src}] {title}"
        if summ and summ.lower() not in title.lower():
            line += f" — {summ}"
        lines.append(line)

    return "\n".join(lines)


# ── Pass 1: identify top symbols ──────────────────────────────────────────────

_PASS1_PROMPT = """\
You are an expert Indian stock market analyst. Below is a list of NSE/BSE listed \
company symbols for reference:

{symbol_ref}

----- TODAY'S NEWS (last 48 hours) -----
{digest}
-----------------------------------------

Your task: Based ONLY on the news above, identify the TOP 5 stocks from the NSE/BSE \
list that have the STRONGEST POSITIVE CATALYST with highest probability of price upside. \
Consider (in order of importance):
  1. Corporate actions — bonus issue, buyback, large dividend, split
  2. M&A — acquisition announcement (acquirer premium), strategic deal
  3. Earnings beat — quarterly results significantly above estimates
  4. Large order wins — government / export / blue-chip client
  5. Regulatory approval — drug approval, licence, tender win
  6. Sector tailwind — government policy, budget allocation, global demand
  7. Analyst upgrade — significant target price raise or upgrade

IGNORE negative news, regulatory penalties, or downgrades.

Respond with EXACTLY this format (nothing else):
PICK 1: SYMBOL | Company Name | Catalyst type | One-sentence reasoning
PICK 2: SYMBOL | Company Name | Catalyst type | One-sentence reasoning
PICK 3: SYMBOL | Company Name | Catalyst type | One-sentence reasoning
PICK 4: SYMBOL | Company Name | Catalyst type | One-sentence reasoning
PICK 5: SYMBOL | Company Name | Catalyst type | One-sentence reasoning

Use only symbols from the reference list. If fewer than 5 stocks have clear positive \
catalysts, output only those that do.
"""

_PASS2_PROMPT = """\
You are a senior Indian equity analyst writing a concise investment note.

Stock: {symbol} — {company}
Catalyst: {catalyst}

Relevant news snippets:
{snippets}

Write a 3-sentence investment thesis explaining:
1. What specifically happened (the catalyst)
2. Why this is positive for the stock price
3. Key risk to watch

Keep each sentence under 25 words. Output only the 3 sentences, no headers.
"""


def _parse_picks(response: str) -> list[dict]:
    """
    Parse PICK N: SYMBOL | Company | Catalyst | Reasoning lines.
    Handles: "PICK N:", "N.", "N)", numbered lists, and bare pipe rows.
    Strips markdown decorators (**bold**, > quote, etc.) before matching.
    """
    picks: list[dict] = []
    seen: set[str] = set()

    for raw_line in response.splitlines():
        line = raw_line.strip()
        # Strip leading markdown decorators
        line = re.sub(r"^[#*>\-_=|`\s]+", "", line).strip()
        if not line:
            continue

        # Extract content after a pick-number prefix, or take the whole line
        m = re.match(r"(?:PICK\s*\d+\s*:?\s*|\d+[.):\s]+)\s*(.+)", line, re.I)
        content = m.group(1) if m else line

        # Must have at least one pipe to be a candidate
        if "|" not in content:
            continue

        parts = [p.strip() for p in content.split("|")]
        # Skip markdown table header/separator rows
        if not parts or parts[0].lower() in {"rank", "symbol", "pick", "#", "no", ""}:
            continue
        if re.fullmatch(r"[-:\s]+", parts[0]):
            continue

        raw_symbol = parts[0].upper().strip()
        # Remove .NS/.BO suffix and any trailing punctuation
        raw_symbol = re.sub(r"\.(NS|BO)$", "", raw_symbol).rstrip(".,:;")
        company    = parts[1].strip() if len(parts) > 1 else ""
        catalyst   = parts[2].strip() if len(parts) > 2 else ""
        reasoning  = parts[3].strip() if len(parts) > 3 else ""

        clean_sym = raw_symbol
        if clean_sym not in _SYMBOL_MAP:
            matches = [s for s in _CLEAN_SYMBOLS if clean_sym in s or s in clean_sym]
            if not matches:
                log.debug(f"analyzer: unknown symbol '{raw_symbol}' — skipping")
                continue
            clean_sym = matches[0]

        if clean_sym in seen:
            continue
        seen.add(clean_sym)

        full_sym, full_name = _SYMBOL_MAP[clean_sym]
        picks.append({
            "symbol"   : full_sym,
            "clean"    : clean_sym,
            "company"  : full_name or company,
            "catalyst" : catalyst,
            "reasoning": reasoning,
        })

    return picks


def _relevant_snippets(symbol_clean: str, company: str, articles: list[dict],
                       max_snips: int = 6) -> str:
    """Return up to max_snips article titles/summaries mentioning this stock."""
    kws = {symbol_clean.lower(), company.lower().split()[0]}
    snips: list[str] = []
    for art in articles:
        text = (art.get("title", "") + " " + art.get("summary", "")).lower()
        if any(kw in text for kw in kws):
            snips.append(f"- {art['title']}")
            if len(snips) >= max_snips:
                break
    return "\n".join(snips) if snips else "- No direct news snippets found; based on sector/market context."


# ── Public API ────────────────────────────────────────────────────────────────

def analyze_news(articles: list[dict], top_n: int = 5) -> list[dict]:
    """
    Runs both passes against Ollama.

    Returns a list of recommendation dicts:
    {symbol, clean, company, catalyst, reasoning, analysis}

    Raises RuntimeError if Ollama is unavailable.
    """
    from config import NEWS_TOP_N
    top_n = top_n or NEWS_TOP_N

    relevant = filter_relevant(articles)
    if not relevant:
        log.warning("analyzer: no relevant articles — skipping analysis")
        return []

    digest = build_digest(relevant, max_articles=80)

    # ── Pass 1: identify top picks ───────────────────────────────────────────
    log.info(f"analyzer: digest={len(digest)} chars  articles={len(relevant)}")
    log.info("analyzer: Pass 1 — identifying top stock picks from news digest")
    prompt1  = _PASS1_PROMPT.format(symbol_ref=_SYMBOL_REF, digest=digest)
    log.info(f"analyzer: Pass 1 prompt size = {len(prompt1)} chars (~{len(prompt1)//4} tokens est.)")
    response = generate(prompt1, timeout=180)
    log.info(f"analyzer: Pass 1 raw response (first 300 chars):\n{response[:300]}")

    picks = _parse_picks(response)
    if not picks:
        log.warning("analyzer: Pass 1 returned no parseable picks")
        log.warning(f"analyzer: raw Pass 1 response (first 1000 chars):\n{response[:1000]}")
        return []

    picks = picks[:top_n]
    log.info(f"analyzer: Pass 1 identified {len(picks)} pick(s): "
             f"{[p['clean'] for p in picks]}")

    # ── Pass 2: deep analysis per pick ──────────────────────────────────────
    for pick in picks:
        snips   = _relevant_snippets(pick["clean"], pick["company"], relevant)
        prompt2 = _PASS2_PROMPT.format(
            symbol   = pick["clean"],
            company  = pick["company"],
            catalyst = pick["catalyst"],
            snippets = snips,
        )
        log.info(f"analyzer: Pass 2 — deep analysis for {pick['clean']}")
        # think=True: free-form thesis benefits from chain-of-thought reasoning, but
        # the model's hidden "thinking" trace length is PROBABILISTIC, not bounded
        # by num_predict — calibration on the same prompt produced traces of 2.9K,
        # 7.9K, and 13.2K chars across runs (the last one exhausted the budget and
        # returned empty after 219s). Raising num_predict to 6144 didn't fix this —
        # it just sometimes made the model think longer for the same outcome — so
        # there's no ceiling that reliably prevents the spiral; 3072 already covers
        # every successful completion observed (max ~2K total tokens) with room to
        # spare. timeout=210s comfortably covers the slowest legitimate completion
        # seen (157.5s, +33% margin) without wasting time waiting out a spiral that
        # could run far longer. Either an empty result or a timeout/exception falls
        # through to a think=False retry — it skips the thinking step entirely and
        # answers directly in ~10-25s; only if THAT also comes up empty do we fall
        # back to the short Pass-1 one-liner. A pick should never ship blank text.
        analysis = ""
        try:
            analysis = generate(prompt2, timeout=210, think=True)
            if not analysis:
                log.warning(f"analyzer: Pass 2 think=True returned empty for "
                            f"{pick['clean']} — retrying with think=False")
        except Exception as exc:
            log.warning(f"analyzer: Pass 2 think=True failed for {pick['clean']} "
                        f"— {exc} — retrying with think=False")

        if not analysis:
            try:
                analysis = generate(prompt2, timeout=120, think=False)
            except Exception as exc:
                log.warning(f"analyzer: Pass 2 think=False fallback also failed "
                            f"for {pick['clean']} — {exc}")
        pick["analysis"] = analysis or pick["reasoning"]

    return picks
