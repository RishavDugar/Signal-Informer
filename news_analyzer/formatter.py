"""
Formats news-based stock picks into a WhatsApp message.

Stays within the 3 800-character limit by truncating analysis and
splitting into multiple parts if needed.
"""

from __future__ import annotations

from datetime import date

MAX_CHARS      = 3_800
# Pass-2 prompts ask for a 3-sentence thesis, each sentence "under ~28-30 words"
# — roughly 90 words / ~550-650 chars with punctuation. 750 gives full theses
# headroom to render uncut while still bounding pathological model output.
# (A previous 220-char limit was chopping theses mid-sentence — e.g. "Some
# reasons. Some investors did this thing,..." — well before they finished.)
# format_messages()/format_scout_messages() already bucket picks across
# multiple WhatsApp parts to stay under MAX_CHARS, so raising this is safe.
ANALYSIS_LIMIT = 750


def _price_line(rec: dict) -> str:
    """Build the CMP / % change line from price context fields."""
    parts: list[str] = []
    cmp = rec.get("cmp")
    if cmp:
        parts.append(f"CMP Rs{cmp:,.1f}")
    for key, label in [("change_1d_pct", "1D"), ("change_5d_pct", "5D"),
                        ("change_20d_pct", "20D")]:
        val = rec.get(key)
        if val is not None:
            sign = "+" if val >= 0 else ""
            parts.append(f"{label}: {sign}{val:.1f}%")
    return "  ".join(parts)


def _technical_line(rec: dict) -> str:
    """Build the RSI / Volume / SMA-trend line from technical context fields."""
    parts: list[str] = []

    rsi = rec.get("rsi14")
    if rsi is not None:
        tag = ""
        if rsi >= 70:
            tag = " (overbought)"
        elif rsi <= 30:
            tag = " (oversold)"
        parts.append(f"RSI(14) {rsi:.0f}{tag}")

    vol_ratio = rec.get("volume_ratio")
    if vol_ratio is not None:
        flag = " (surge)" if vol_ratio >= 2 else ""
        parts.append(f"Vol {vol_ratio:.1f}x avg{flag}")

    trend = rec.get("sma20_trend")
    sma_pct = rec.get("sma20_pct")
    if trend and sma_pct is not None:
        sign = "+" if sma_pct >= 0 else ""
        parts.append(f"{trend.title()} 20-SMA ({sign}{sma_pct:.1f}%)")

    return "  ".join(parts)


def _catalyst_tag(catalyst: str) -> str:
    c = catalyst.lower()
    if any(w in c for w in ("bonus", "split", "dividend", "buyback")):
        return "CORPORATE ACTION"
    if any(w in c for w in ("acqui", "merger", "deal", "takeover")):
        return "M&A"
    if any(w in c for w in ("result", "earning", "profit", "revenue", "quarter")):
        return "EARNINGS"
    if any(w in c for w in ("order", "contract", "win", "tender")):
        return "ORDER WIN"
    if any(w in c for w in ("approv", "licence", "regulat")):
        return "APPROVAL"
    if any(w in c for w in ("analyst", "upgrade", "target", "rating")):
        return "UPGRADE"
    return "CATALYST"


def _build_part(recs: list[dict], run_date: str, part: int, total: int) -> str:
    lines: list[str] = []

    hdr = f"*NEWS PICKS  {run_date}*"
    if total > 1:
        hdr += f"  (Part {part}/{total})"
    lines += [hdr, "_Fundamental + news catalyst analysis  |  Ollama AI_", ""]

    for i, rec in enumerate(recs, 1):
        clean   = rec.get("clean", rec["symbol"].replace(".NS","").replace(".BO",""))
        company = rec.get("company_name", rec.get("company", clean))
        ctag    = _catalyst_tag(rec.get("catalyst", ""))

        lines.append(f"*{i}. {clean}*  [{ctag}]")
        lines.append(f"{company}")
        lines.append(f"Catalyst: {rec.get('catalyst', '')}")

        price = _price_line(rec)
        if price:
            lines.append(price)
        tech = _technical_line(rec)
        if tech:
            lines.append(tech)

        analysis = (rec.get("analysis") or "").strip()
        if analysis:
            if len(analysis) > ANALYSIS_LIMIT:
                analysis = analysis[:ANALYSIS_LIMIT].rsplit(" ", 1)[0] + "..."
            lines.append(analysis)
        lines.append("")

    return "\n".join(lines).strip()


def _scout_pick_lines(pick: dict, index: int, tag: str) -> list[str]:
    clean   = pick.get("clean", pick["symbol"].replace(".NS","").replace(".BO",""))
    company = pick.get("company", clean)
    catalyst= pick.get("catalyst", "")
    analysis= (pick.get("analysis") or pick.get("reasoning", "")).strip()
    if len(analysis) > ANALYSIS_LIMIT:
        analysis = analysis[:ANALYSIS_LIMIT].rsplit(" ", 1)[0] + "..."

    lines = [f"*{index}. {clean}*  [{tag}]", company]
    if catalyst:
        lines.append(f"Edge: {catalyst}")

    price = _price_line(pick)
    if price:
        lines.append(price)
    tech = _technical_line(pick)
    if tech:
        lines.append(tech)

    if analysis:
        lines.append(analysis)
    return lines


def format_scout_messages(picks: list[dict], label: str, tag: str,
                          subtitle: str = "", run_date: str | None = None) -> list[str]:
    """
    Format a list of scout-lens picks (hidden gems / small-cap growth / smart money)
    as one or more WhatsApp messages, including price + technical-indicator context.

    label    — message header, e.g. "SCOUT PICKS — Hidden Gems"
    tag      — per-stock badge, e.g. "HIDDEN GEM"
    subtitle — italic subtitle line under the header
    """
    if not picks:
        return []
    run_date = run_date or date.today().strftime("%d %b %Y")
    subtitle = subtitle or "Ollama AI"

    lines = [f"*{label}  {run_date}*", f"_{subtitle}_", ""]
    for i, pick in enumerate(picks, 1):
        lines.extend(_scout_pick_lines(pick, i, tag))
        lines.append("")

    msg = "\n".join(lines).strip()
    if len(msg) <= MAX_CHARS:
        return [msg]

    # Split: one pick per message if the combined message exceeds the limit
    msgs: list[str] = []
    for i, pick in enumerate(picks, 1):
        part_lines = [f"*{label}  {run_date}  ({i}/{len(picks)})*", f"_{subtitle}_", ""]
        part_lines.extend(_scout_pick_lines(pick, i, tag))
        msgs.append("\n".join(part_lines).strip())
    return msgs


def format_messages(recs: list[dict], run_date: str | None = None) -> list[str]:
    """
    Return a list of WhatsApp message strings, each within MAX_CHARS.
    Splits into multiple parts automatically.
    """
    if not recs:
        return []
    run_date = run_date or date.today().strftime("%d %b %Y")

    # Try fitting all recs in one message; split if needed
    msgs: list[str] = []
    bucket: list[dict] = []

    for rec in recs:
        trial = _build_part(bucket + [rec], run_date, 1, 1)
        if len(trial) > MAX_CHARS and bucket:
            msgs.append(bucket)
            bucket = [rec]
        else:
            bucket.append(rec)

    if bucket:
        msgs.append(bucket)

    total = len(msgs)
    return [_build_part(m, run_date, i + 1, total) for i, m in enumerate(msgs)]
