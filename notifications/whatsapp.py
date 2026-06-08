"""
WhatsApp notifier.

Two send backends (config.WHATSAPP_BACKEND):
  "bridge"    — headless Node service (whatsapp-web.js / Puppeteer). Sends over
                the WhatsApp Web protocol, so it works with the screen OFF and
                the device LOCKED. Recommended. See notifications/whatsapp_bridge/.
  "pywhatkit" — legacy GUI automation; only works while the desktop is unlocked
                and focused (the OS blocks synthetic keystrokes when locked, so
                the message gets typed but the Enter to send never lands).

Message format (send_batch_signal_alert):
  Per stock: plain-English direction, conviction, net expectancy, track record,
  risk, and one bullet per firing setup. All figures are net of estimated costs.
"""

import math
import subprocess
import time
from typing import Optional

import requests

from config import (
    WHATSAPP_PHONE, WHATSAPP_PHONES, MIN_AVG_RETURN, MIN_CONFIDENCE,
    WHATSAPP_BACKEND, WHATSAPP_BRIDGE_URL, WHATSAPP_BRIDGE_TOKEN,
    WHATSAPP_BRIDGE_AUTOSTART, WHATSAPP_BRIDGE_DIR, WHATSAPP_BRIDGE_READY_TIMEOUT,
)
from utils.logger import get_logger

log = get_logger(__name__)

WAIT_TIME  = 15    # seconds for WhatsApp Web to load before sending (pywhatkit)
CLOSE_TIME = 3     # seconds before tab closes after send (pywhatkit)
MAX_CHARS  = 3800  # per message; oversized messages are split
# MIN_AVG_RETURN is loaded from config (set via MIN_AVG_RETURN in .env)


# ── Setup catalogue ───────────────────────────────────────────────────────────
# (description, stop_hint, target_hint) — empty string means not defined

_CATALOGUE: dict[str, tuple[str, str, str]] = {
    "RSI_EXTREME": (
        "RSI overbought (>70) or oversold (<30) signal",
        "", "",
    ),
    "TURTLE_SOUP": (
        "New 20-day extreme; prior extreme >=4 sessions ago — false-breakout reversal",
        "1 tick beyond today's new extreme",
        "Momentum reversal confirmation",
    ),
    "TURTLE_SOUP_PLUS_ONE": (
        "Day-after Turtle Soup — late breakout participants trapped",
        "Below day-1 / day-2 extreme",
        "Continuation after entry level breaks",
    ),
    "EIGHTY_TWENTY": (
        "Prior day opened at one extreme, closed at the opposite — midday reversal setup",
        "Today's intraday extreme (opposite side of entry)",
        "Close of the reversal session",
    ),
    "MOMENTUM_PINBALL": (
        "3-period RSI of 1-day ROC <=30 (buy) / >=70 (sell) — LBR/RSI Day-1",
        "Low of entry-day's first hour",
        "Next morning's open or follow-through close",
    ),
    "TWO_PERIOD_ROC": (
        "2-period ROC pivot flip — Taylor 2-3 day swing rhythm",
        "Close back through pivot level",
        "Exit next session on follow-through",
    ),
    "THE_ANTI": (
        "Stochastic %K hooks in direction of trending %D — retracement continuation",
        "Just below entry bar",
        "Buying/selling climax within 3-4 sessions",
    ),
    "HOLY_GRAIL": (
        "ADX(14)>30 rising + price pulls back to 20-EMA — trend continuation",
        "Retracement swing low (in metadata)",
        "Prior swing high (resistance)",
    ),
    "ADX_GAPPER": (
        "Gap against a strong ADX trend — reversal back in trend direction",
        "Today's low (buy) / high (sell)",
        "Trail stop; trend continuation",
    ),
    "WHIPLASH": (
        "Gap beyond prior extreme + close reverses into opposing half of range",
        "Exit if next open is adverse",
        "Follow-through on next morning's open",
    ),
    "THREE_DAY_GAP_REVERSAL": (
        "Unfilled gap within 3 sessions — gap-fill reversal play",
        "Gap-day extreme (see entry stop)",
        "Prior session's extreme (fill level)",
    ),
    "ID_NR4": (
        "Inside Day + Narrowest Range of 4 bars — imminent volatility expansion",
        "1 tick below today's low (long) / above high (short)",
        "No fixed target — trail on breakout",
    ),
    "HV_NR4": (
        "6-day HV < 50% of 100-day HV + Inside/NR4 — ultra-low volatility explosion",
        "1 tick below today's low (long) / above high (short)",
        "No fixed target — trail on breakout",
    ),
}


# ── Plain-English names + one-line investor rationale ─────────────────────────
# Investors don't read code names like "TURTLE_SOUP" or stochastic jargon. Each
# entry is (friendly_name, one_line_why) — what the strategy is, in a sentence a
# non-trader can act on. Used in the stock-pick alert.

_PLAIN: dict[str, tuple[str, str]] = {
    "RSI_EXTREME":            ("Oversold/Overbought Bounce",
        "Price ran too far, too fast and is stretched — history favours a snap back the other way."),
    "TURTLE_SOUP":            ("False-Breakout Reversal",
        "Price poked to a fresh 20-day extreme then failed — the breakout looks fake and tends to reverse."),
    "TURTLE_SOUP_PLUS_ONE":   ("False-Breakout Reversal (Day 2)",
        "A failed breakout that traps late buyers/sellers one day later — the reversal often follows."),
    "EIGHTY_TWENTY":          ("Intraday Reversal",
        "Yesterday opened at one extreme and closed at the other — that exhaustion often reverses next session."),
    "MOMENTUM_PINBALL":       ("Short-Term Momentum Turn",
        "Very short-term momentum is exhausted; price tends to swing back over the next 1-2 days."),
    "TWO_PERIOD_ROC":         ("2-Day Swing Turn",
        "A 2-day momentum flip that captures the natural buy-day / sell-day rhythm."),
    "THE_ANTI":               ("Trend Pullback Continuation",
        "A brief pullback inside a clear trend is ending — the trend looks set to resume."),
    "HOLY_GRAIL":             ("Strong-Trend Re-Entry",
        "A strong trend dipped to its 20-day average — a classic lower-risk spot to join the move."),
    "ADX_GAPPER":             ("Trend Gap Snap-Back",
        "Price gapped against a strong trend; history shows it tends to recover in the trend direction."),
    "WHIPLASH":               ("Gap-and-Reverse",
        "Price gapped through yesterday's extreme then reversed hard — the gap exhausted one side."),
    "THREE_DAY_GAP_REVERSAL": ("Unfilled-Gap Reversal",
        "An unfilled gap is starting to close within 3 days — a sign the original move is exhausting."),
    "ID_NR4":                 ("Volatility Squeeze Breakout",
        "The range has coiled to its tightest in days — a sharp expansion move usually follows."),
    "HV_NR4":                 ("Deep Volatility Squeeze",
        "Volatility is at a multi-month low and coiled — these precede some of the year's biggest moves."),
    "BOLLINGER_SQUEEZE":      ("Bollinger Squeeze Breakout",
        "Volatility bands have pinched tight — a directional breakout typically follows the squeeze."),
    "EMA_TREND_PULLBACK":     ("Moving-Average Pullback",
        "An established trend pulled back to a key moving average — a spot to rejoin the trend."),
    "MACD_DIVERGENCE":        ("Momentum Divergence",
        "Price made a new extreme but momentum didn't confirm — a warning the move is tiring."),
    "N_DOWN_REVERSAL":        ("Multi-Day Reversal",
        "Several down days in a row have over-extended price — a bounce is statistically due."),
    "VOLUME_CLIMAX":          ("Volume Climax Reversal",
        "A spike of panic/euphoria volume often marks the end of a move and a turn."),
}


def _plain_name(code: str) -> str:
    """Friendly strategy name for investors; prettifies the code if unmapped."""
    if code in _PLAIN:
        return _PLAIN[code][0]
    return code.replace("_", " ").title()


def _plain_why(code: str) -> str:
    """One-line plain-English rationale; falls back to the catalogue description."""
    if code in _PLAIN:
        return _PLAIN[code][1]
    return _CATALOGUE.get(code, ("", "", ""))[0]


# ── Setup guide ──────────────────────────────────────────────────────────────
# Full strategy explanations sent as a primer before the stock picks.
# Format per entry: (title, concept, entry_rule, stop_rule, target_rule)

_SETUP_GUIDE: dict[str, tuple[str, str, str, str, str]] = {
    "RSI_EXTREME": (
        "RSI Extreme",
        (
            "The Relative Strength Index (RSI) measures momentum. When RSI "
            "falls below 30 the market is oversold — sellers are exhausted. "
            "When it rises above 70 the market is overbought — buyers are "
            "exhausted. Both conditions set up a potential mean-reversion move."
        ),
        "Wait for RSI to pierce the threshold then react on the next bar.",
        "Close beyond the extreme that triggered the signal (new high/low).",
        "Reversal towards the midpoint; exit within 2-4 sessions.",
    ),
    "TURTLE_SOUP": (
        "Turtle Soup",
        (
            "The Turtles bought every 20-day high and sold every 20-day low. "
            "Turtle Soup does the opposite: when the market makes a new 20-day "
            "extreme but the PRIOR 20-day extreme was at least 4 sessions ago, "
            "the breakout is likely FALSE. Smart money traps the late momentum "
            "players and the market reverses sharply."
        ),
        (
            "After today's new 20-day extreme, place a stop-entry 5-10 ticks "
            "back inside the prior 20-day extreme level. Good for today only."
        ),
        "1 tick beyond today's new extreme (the farthest point).",
        "Trail stop; some reversals become multi-week trend changes.",
    ),
    "TURTLE_SOUP_PLUS_ONE": (
        "Turtle Soup Plus One",
        (
            "Identical concept to Turtle Soup but triggered one day LATER. "
            "The setup day's close must be at or beyond the prior 20-day extreme, "
            "trapping even more breakout followers who entered on the close. "
            "Advantage: you know the evening before whether a setup exists."
        ),
        (
            "Day two: place entry stop at the prior 20-day extreme level. "
            "If not filled on day two, cancel the trade."
        ),
        "1 tick below the lower of day-1 or day-2 extreme.",
        "Partial profits within 2-6 bars; trail stop on remainder.",
    ),
    "EIGHTY_TWENTY": (
        "80-20s",
        (
            "When a market opens in the TOP 20% of its range and closes in the "
            "BOTTOM 20% (or vice versa), it has demonstrated extreme intraday "
            "range behaviour. Research shows that 80-90% of the time the next "
            "day will breach the prior high/low, but only ~50% of the time does "
            "it actually CLOSE higher/lower. The midday reversal tendency is the "
            "edge. Day-trade only."
        ),
        (
            "Next day: if the market breaches the prior session's extreme by "
            "5-15 ticks and then reverses back, enter at the prior day's extreme."
        ),
        "Today's intraday extreme (the false-breakout point).",
        "Close of the reversal session; do NOT hold overnight.",
    ),
    "MOMENTUM_PINBALL": (
        "Momentum Pinball (LBR/RSI)",
        (
            "Calculate the 1-day price change (momentum). Run a 3-period RSI "
            "on THAT momentum series — this is the LBR/RSI. A reading below 30 "
            "signals buyers are temporarily exhausted on a 2-3 day basis; above "
            "70 signals sellers are exhausted. The setup captures Taylor's "
            "natural buy-day / sell-day rhythm."
        ),
        (
            "Day-1: LBR/RSI < 30 (buy) or > 70 (sell). "
            "Day-2: enter on a breakout of the first hour's trading range in "
            "the signal direction."
        ),
        "Low (buy) or high (sell) of the entry day's first hour.",
        "Exit next morning on follow-through; close position by end of day-3.",
    ),
    "TWO_PERIOD_ROC": (
        "2-Period Rate of Change",
        (
            "Measures the 2-day momentum and calculates a short-term pivot. "
            "When today's close is above the pivot and the 2-period ROC just "
            "flipped positive, go home long. When below the pivot and the ROC "
            "just flipped negative, go home short. Captures the same 2-3 day "
            "Taylor swing rhythm as Momentum Pinball but from the daily close."
        ),
        (
            "Enter at/near today's close when the 2-period ROC flips direction "
            "and the close is on the correct side of the pivot."
        ),
        "Close back through the pivot level in the opposite direction.",
        "Exit on the following day's open or close.",
    ),
    "THE_ANTI": (
        "The Anti",
        (
            "Uses a 7-period %K and 10-period %D stochastic. The SLOPE of %D "
            "defines the trend. During a pullback, %K moves against %D. When "
            "%K hooks BACK in the direction of %D (forming a 'hook'), a high-"
            "probability continuation move is about to start. Best when the "
            "%K correction lasts 2-3 bars before hooking."
        ),
        (
            "Enter at-market when %K hooks in the direction of the %D slope. "
            "Alternatively, place a stop above/below the retracement range."
        ),
        "Just below (long) or above (short) the bar of entry.",
        "Exit on a buying/selling climax bar within 3-4 sessions.",
    ),
    "HOLY_GRAIL": (
        "The Holy Grail",
        (
            "When ADX(14) is above 30 AND rising, the market is in a strong "
            "trend. Price then pulls back to the 20-period EMA — the first "
            "retracement in a strongly trending move. Buy the touch of the EMA "
            "because the trend resumption creates a new continuation leg. "
            "ADX will often dip during the retracement, which is normal."
        ),
        (
            "When price touches the 20-EMA, place a buy (sell) stop above "
            "(below) the high (low) of the previous bar."
        ),
        "Newly formed retracement swing low/high (in signal metadata).",
        "Most recent prior swing high (long) or swing low (short).",
    ),
    "ADX_GAPPER": (
        "ADX Gapper",
        (
            "When ADX(12) > 30 and the +DI/-DI confirms the trend direction, "
            "the trend is strong. If the market then GAPS against the trend "
            "(e.g., gaps down in an uptrend), smart money uses the weakness to "
            "add to trend positions. The gap reversal back in the trend direction "
            "has a positive statistical expectation."
        ),
        (
            "Buy: today's open gaps below yesterday's low. Place buy stop at "
            "yesterday's low. Sell: gaps above yesterday's high; sell stop at "
            "yesterday's high."
        ),
        "Today's gap-open low (buy) or high (sell).",
        "Trail stop; carry overnight if closes strongly in trend direction.",
    ),
    "WHIPLASH": (
        "Whiplash",
        (
            "The market gaps beyond the prior session's extreme (down through "
            "the prior low, or up through the prior high), then REVERSES and "
            "closes in the upper/lower 50% of the day's range. This pattern of "
            "gap-and-reverse shows the gap exhausted sellers/buyers. The gap "
            "does NOT need to be filled. Enter at the close (MOC)."
        ),
        (
            "Buy: gap lower + close > open AND in top 50% of range → buy MOC. "
            "Sell: gap higher + close < open AND in bottom 50% → sell MOC."
        ),
        "If next open is adverse (position is immediately at a loss), exit at market.",
        "Next morning's follow-through open; often hold 1-3 days.",
    ),
    "THREE_DAY_GAP_REVERSAL": (
        "Three-Day Unfilled Gap Reversal",
        (
            "When the market gaps and does NOT fill the gap on the gap day, "
            "the gap represents a significant price dislocation. If within the "
            "NEXT THREE SESSIONS the market begins to close the gap, it signals "
            "exhaustion of the gap-direction move and a potential reversal. "
            "A gap that hasn't closed in 3 days is effectively abandoned."
        ),
        (
            "Place a stop-entry one tick beyond the gap day's high (for sell "
            "gaps) or low (for buy gaps). Keep the stop open for 3 sessions only."
        ),
        "Gap-day extreme (the far end of the gap).",
        "Prior session's extreme — the gap fill level.",
    ),
    "ID_NR4": (
        "Inside Day + Narrowest Range 4 (ID/NR4)",
        (
            "An Inside Day has a high LOWER than yesterday's high and a low "
            "HIGHER than yesterday's low — the market is coiling. An NR4 day "
            "has the NARROWEST range of the last four days. Combining both "
            "signals extreme volatility compression. Research shows that "
            "expansion almost always follows. The direction is unknown — both "
            "a buy-stop and sell-stop are placed."
        ),
        (
            "Next day only: buy stop 1 tick above today's high, sell stop "
            "1 tick below today's low. If filled, add a reverse stop in case "
            "of a false breakout (whipsaw and reverse)."
        ),
        "The ID/NR4 bar's opposite extreme (used as the whipsaw-reverse level).",
        "No fixed target — trail stop aggressively; expect a 1-4 day expansion.",
    ),
    "HV_NR4": (
        "Historical Volatility + Toby Crabel (HV/NR4)",
        (
            "Same coiling conditions as ID/NR4 (Inside Day or NR4), with an "
            "additional filter: the 6-day historical volatility is less than "
            "50% of the 100-day HV. This mathematically identifies periods of "
            "HISTORICALLY low volatility — not just a quiet day but a multi-"
            "month extreme in compression. These setups precede some of the "
            "biggest 1-4 day moves of the year."
        ),
        (
            "Day two: buy stop 1 tick above day-one high, sell stop 1 tick "
            "below day-one low. Reverse-stop also placed for same-day whipsaw."
        ),
        "Day-one bar's opposite extreme.",
        "No fixed target — trail stop; occasionally multi-week trend starters.",
    ),
}


def _build_setup_guide_messages(triggered_setup_names: list[str]) -> list[str]:
    """
    Build one or more WhatsApp messages explaining each triggered strategy.
    Returns a list of messages (split at MAX_CHARS if needed).
    """
    unique = []
    seen: set[str] = set()
    for name in triggered_setup_names:
        if name not in seen and name in _SETUP_GUIDE:
            unique.append(name)
            seen.add(name)

    if not unique:
        return []

    lines: list[str] = [
        "*Strategy Guide*",
        f"_{len(unique)} setup(s) triggered today_",
        "",
    ]

    for name in unique:
        title, concept, entry, stop, target = _SETUP_GUIDE[name]
        lines += [
            f"*{title}*",
            f"_{concept}_",
            f"  Entry: {entry}",
            f"  Stop:  {stop}",
            f"  Target: {target}",
            "",
        ]

    # Split into MAX_CHARS chunks if necessary
    messages: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    for line in lines:
        addition = len(line) + 1  # +1 for newline
        if current_len + addition > MAX_CHARS and current_lines:
            messages.append("\n".join(current_lines))
            current_lines = []
            current_len = 0
        current_lines.append(line)
        current_len += addition
    if current_lines:
        messages.append("\n".join(current_lines))

    return messages


# ── Conviction ranking ───────────────────────────────────────────────────────

def _direction_of(sig: dict) -> str:
    """
    Extract the trading direction from a signal dict.
    Returns 'buy', 'sell', or 'neutral'.
    """
    meta = sig.get("metadata", {})
    st   = str(meta.get("signal_type", "")).lower()
    if st in ("buy", "long"):
        return "buy"
    if st in ("sell", "short"):
        return "sell"
    # RSI_EXTREME stores direction in 'condition'
    cond = str(meta.get("condition", "")).lower()
    if cond == "oversold":
        return "buy"
    if cond == "overbought":
        return "sell"
    # ID_NR4 / HV_NR4 / TWO_PERIOD_ROC neutral flips are counted separately
    return "neutral"


def _load_weights() -> dict[str, dict]:
    """
    Load directional conviction weights.
    Returns {setup_name: {"long": float, "short": float, "overall": float}}.
    Falls back to flat {setup_name: 1.0} if old format detected.
    """
    try:
        from backtester import load_directional_weights
        w = load_directional_weights()
        if w:
            return w
    except Exception:
        pass
    return {}


def _load_stats() -> dict[str, dict]:
    """Load full backtested stats {setup_name: {long: ..., short: ..., ...}}."""
    try:
        from backtester import load_stats
        return load_stats()
    except Exception:
        return {}


def _dir_stats(setup_name: str, signal_direction: str, stats: dict) -> dict:
    """
    Return the direction-appropriate stats sub-dict for a setup.
    BUY / neutral / long  -> stats['long']
    SELL / short          -> stats['short']
    Falls back to the flat top-level dict for old JSON files.
    """
    info = stats.get(setup_name, {})
    if signal_direction in ("sell", "short", "overbought"):
        return info.get("short", info)
    return info.get("long", info)


def _get_w(weights: dict, name: str, direction: str) -> float:
    """Extract direction-specific weight, tolerating both flat and nested formats."""
    w = weights.get(name, 1.0)
    if isinstance(w, dict):
        return w.get("long" if direction != "sell" else "short", w.get("overall", 1.0))
    return float(w)


def rank_by_conviction(signals: list[dict], top_n: int = 10) -> list[tuple]:
    """
    Group signals by symbol, score by WEIGHTED same-direction conviction,
    and return the top `top_n` stocks sorted by score descending.

    Scoring formula per stock
    -------------------------
    weights  = from backtester.json (default 1.0 if not yet run)
    buy_w    = sum of weights for buy-direction signals
    sell_w   = sum of weights for sell-direction signals
    neutral_w= sum of weights × 0.5 for directionally ambiguous signals

    dominant = 'BUY'  if buy_w  >= sell_w
               'SELL' if sell_w >  buy_w

    score    = max(buy_w, sell_w)           # dominant-side conviction
             + neutral_w                    # neutral amplifies either side
             - min(buy_w, sell_w) * 0.25   # slight penalty for contradicting signals

    Returns list of (symbol, sigs, dominant, score) — length <= top_n.
    """
    weights = _load_weights()

    by_sym: dict[str, list[dict]] = {}
    for sig in signals:
        by_sym.setdefault(sig["symbol"], []).append(sig)

    ranked = []
    for symbol, sigs in by_sym.items():
        buy_w  = sum(_get_w(weights, s["setup_name"], "buy")
                     for s in sigs if _direction_of(s) == "buy")
        sell_w = sum(_get_w(weights, s["setup_name"], "sell")
                     for s in sigs if _direction_of(s) == "sell")
        neut_w = sum(_get_w(weights, s["setup_name"], "neutral") * 0.5
                     for s in sigs if _direction_of(s) == "neutral")

        dominant = "BUY" if buy_w >= sell_w else "SELL"
        score    = (max(buy_w, sell_w)
                    + neut_w
                    - min(buy_w, sell_w) * 0.25)

        ranked.append((symbol, sigs, dominant, round(score, 2)))

    ranked.sort(key=lambda x: x[3], reverse=True)
    return ranked[:top_n]


def _stock_metric(sigs: list[dict], stats: dict, weights: dict,
                  field: str, default: float) -> float:
    """
    Conviction-weighted average of a direction-specific backtested `field`
    across all setups firing on one stock. Each setup is weighted by its
    backtested conviction weight, so stronger setups dominate the blended number.
    Returns `default` when no backtest data is available.
    """
    total_w = total_x_w = 0.0
    for sig in sigs:
        name      = sig["setup_name"]
        direction = _direction_of(sig)
        w = _get_w(weights, name, direction)
        x = _dir_stats(name, direction, stats).get(field, default)
        if x is None:
            x = default
        total_x_w += w * x
        total_w   += w
    return total_x_w / total_w if total_w > 0 else default


def _stock_avg_return(sigs, stats, weights) -> float:
    """Conviction-weighted NET expected return per trade."""
    return _stock_metric(sigs, stats, weights, "best_avg_return", 0.0)


def _stock_confidence(sigs, stats, weights) -> float:
    """Conviction-weighted historical win rate (point estimate). 0.50 prior."""
    return _stock_metric(sigs, stats, weights, "best_confidence", 0.5)


def _stock_wr_lower(sigs, stats, weights) -> float:
    """Conviction-weighted Wilson lower-bound win rate — the worst-case floor."""
    return _stock_metric(sigs, stats, weights, "best_wr_lower", 0.0)


def _stock_avg_loss(sigs, stats, weights) -> float:
    """Conviction-weighted average losing-trade return (negative)."""
    return _stock_metric(sigs, stats, weights, "avg_loss", 0.0)


def _stock_sl_rate(sigs, stats, weights) -> float:
    """Conviction-weighted stop-loss hit rate."""
    return _stock_metric(sigs, stats, weights, "sl_rate", 0.0)


def _conviction_stars(wr_lower: float, net_ret: float) -> str:
    """
    Map the honest reliability floor (Wilson lower-bound win rate) to a 1-5
    rating an investor can read at a glance. A negative net expected return is
    capped at 1 star regardless of win rate — magnitude can sink a high hit rate.
    """
    if net_ret <= 0:
        n = 1
    elif wr_lower >= 0.60:
        n = 5
    elif wr_lower >= 0.55:
        n = 4
    elif wr_lower >= 0.50:
        n = 3
    elif wr_lower >= 0.45:
        n = 2
    else:
        n = 1
    return "●" * n + "○" * (5 - n)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _p(v) -> str:
    """Format a price value cleanly. Returns '—' for None/missing."""
    if v is None:
        return "—"
    try:
        f = float(v)
        if math.isnan(f):
            return "—"
        return f"{f:.2f}" if f < 100 else f"{f:.0f}"
    except (TypeError, ValueError):
        return str(v)


def _ohlc_line(symbol: str) -> str:
    """Fetch the latest stored bar and return a single OHLC line."""
    try:
        from data.db import get_ohlcv
        df = get_ohlcv(symbol, days=3)
        if df.empty or len(df) < 1:
            return ""
        last     = df.iloc[-1]
        prev_cls = float(df["close"].iloc[-2]) if len(df) >= 2 else float("nan")
        dt       = df.index[-1].strftime("%d %b")
        o, h, l, c = (float(last[k]) for k in ("open", "high", "low", "close"))
        chg = c - prev_cls
        if not math.isnan(chg):
            chg_str = f"  Chg: {'+' if chg >= 0 else ''}{_p(chg)}"
        else:
            chg_str = ""
        return f"_OHLC {dt}: O {_p(o)}  H {_p(h)}  L {_p(l)}  C {_p(c)}{chg_str}_"
    except Exception:
        return ""


def _meta_lines(setup_name: str, meta: dict) -> list[str]:
    """Return indented detail lines for a setup's metadata + stop/target."""
    lines: list[str] = []

    def row(label: str, val) -> None:
        lines.append(f"  {label}: {val}")

    if setup_name == "RSI_EXTREME":
        row("RSI",       f"{meta.get('rsi', '?')} (period {meta.get('period', '?')})")
        row("Condition", str(meta.get("condition", "?")).upper())
        lo = meta.get("threshold_low", 30)
        hi = meta.get("threshold_high", 70)
        if meta.get("condition") == "oversold":
            row("Stop", f"Below {lo} RSI breakdown level")
        else:
            row("Stop", f"Above {hi} RSI breakdown level")

    elif setup_name == "TURTLE_SOUP":
        row("Direction",      str(meta.get("signal_type", "?")).upper())
        row("New extreme",    _p(meta.get("new_extreme")))
        row("Prior extreme",  _p(meta.get("prev_extreme")))
        row("Sessions apart", meta.get("days_since_prev", "?"))
        row("Entry",          f"5-10 ticks beyond {_p(meta.get('prev_extreme'))}")
        row("Stop",           f"1 tick beyond {_p(meta.get('new_extreme'))}")

    elif setup_name == "TURTLE_SOUP_PLUS_ONE":
        row("Direction",    str(meta.get("signal_type", "?")).upper())
        row("Entry level",  _p(meta.get("entry_level")))
        row("Setup day",    meta.get("setup_day", "?"))
        row("Stop",         f"Below/above entry zone ~{_p(meta.get('entry_level'))}")

    elif setup_name == "EIGHTY_TWENTY":
        op  = meta.get("open_pct", 0)
        cl  = meta.get("close_pct", 0)
        row("Direction",      str(meta.get("signal_type", "?")).upper())
        row("Open % in range", f"{op:.0%}")
        row("Close % in range",f"{cl:.0%}")
        row("Entry stop",     _p(meta.get("entry_level")))
        row("Stop",           "Today's intraday extreme")

    elif setup_name == "MOMENTUM_PINBALL":
        row("Direction", str(meta.get("signal_type", "?")).upper())
        row("LBR/RSI",   meta.get("lbr_rsi", "?"))
        row("1-day ROC", meta.get("roc_1",   "?"))
        row("Entry",     "Buy/sell stop on next day's first-hour breakout")
        row("Stop",      "Low / high of entry-day's first hour")

    elif setup_name == "TWO_PERIOD_ROC":
        row("Direction",      str(meta.get("signal_type", "?")).upper())
        row("2-period ROC",   meta.get("roc_2", "?"))
        row("Pivot tomorrow", _p(meta.get("pivot_tomorrow")))
        row("Today's close",  _p(meta.get("close")))
        row("Stop",           f"Close back through pivot {_p(meta.get('pivot_tomorrow'))}")

    elif setup_name == "THE_ANTI":
        row("Direction", str(meta.get("signal_type", "?")).upper())
        row("%K",        round(float(meta.get("pct_k", 0)), 1))
        row("%D",        round(float(meta.get("pct_d", 0)), 1))
        row("%D slope",  round(float(meta.get("d_slope", 0)), 3))
        row("Stop",      "Just below / above entry bar")
        row("Target",    "Exit on climax bar within 3-4 sessions")

    elif setup_name == "HOLY_GRAIL":
        row("Direction", str(meta.get("signal_type", "?")).upper())
        row("ADX(14)",   round(float(meta.get("adx", 0)), 1))
        row("EMA(20)",   _p(meta.get("ema20")))
        row("+DI / -DI", f"{round(float(meta.get('plus_di',0)),1)} / "
                         f"{round(float(meta.get('minus_di',0)),1)}")
        row("Stop",      _p(meta.get("entry_stop")))
        row("Target",    "Most recent prior swing high / low")

    elif setup_name == "ADX_GAPPER":
        row("Direction",   str(meta.get("signal_type", "?")).upper())
        row("ADX(12)",     round(float(meta.get("adx", 0)), 1))
        row("+DI / -DI",   f"{round(float(meta.get('plus_di',0)),1)} / "
                           f"{round(float(meta.get('minus_di',0)),1)}")
        row("Gap size",    _p(meta.get("gap_size")))
        row("Entry level", _p(meta.get("entry_level")))
        row("Stop",        "Today's low (buy) / high (sell)")

    elif setup_name == "WHIPLASH":
        row("Direction",        str(meta.get("signal_type", "?")).upper())
        row("Gap %",            f"{float(meta.get('gap_pct', 0)):.1%}")
        row("Close % in range", f"{float(meta.get('close_pct', 0)):.0%}")
        row("Stop",             "Exit immediately if next open is adverse")

    elif setup_name == "THREE_DAY_GAP_REVERSAL":
        row("Direction",   str(meta.get("signal_type", "?")).upper())
        row("Gap day",     meta.get("gap_day", "?"))
        row("Days since",  meta.get("days_since_gap", "?"))
        row("Entry stop",  _p(meta.get("gap_level")))
        row("Target",      _p(meta.get("fill_level")))
        row("Stop",        f"Beyond gap-day extreme {_p(meta.get('gap_level'))}")

    elif setup_name in ("ID_NR4", "HV_NR4"):
        flags = []
        if meta.get("is_inside_day"):
            flags.append("Inside Day")
        if meta.get("is_nr4"):
            flags.append("NR4")
        row("Conditions", " + ".join(flags) if flags else "—")
        row("Today High",  _p(meta.get("today_high")))
        row("Today Low",   _p(meta.get("today_low")))
        row("Range",       _p(meta.get("range_size")))
        if setup_name == "HV_NR4":
            row("HV ratio",
                f"{float(meta.get('hv_ratio', 0)):.2f}  (threshold < 0.50)")
        row("Long stop",  f"Below {_p(meta.get('today_low'))}")
        row("Short stop", f"Above {_p(meta.get('today_high'))}")

    else:
        # Generic fallback
        for k, v in meta.items():
            if k != "error":
                row(k.replace("_", " ").title(), v)

    return lines


# ── Send backends ──────────────────────────────────────────────────────────────

_bridge_started = False   # process-local guard so we attempt autostart only once


def _bridge_headers() -> dict:
    return {"X-Token": WHATSAPP_BRIDGE_TOKEN} if WHATSAPP_BRIDGE_TOKEN else {}


def _bridge_ready() -> bool:
    """True if the Node bridge is reachable AND its WhatsApp client is logged in."""
    try:
        r = requests.get(f"{WHATSAPP_BRIDGE_URL}/status",
                         headers=_bridge_headers(), timeout=5)
        return bool(r.ok and r.json().get("ready"))
    except Exception:
        return False


def _autostart_bridge() -> bool:
    """
    Launch the Node bridge detached (headless) if it isn't already running, then
    wait for it to authenticate from its saved session. Returns True once ready.

    Requires a one-time interactive QR scan beforehand (`npm start` in the bridge
    dir) — after that the LocalAuth session persists and headless starts need no QR.
    """
    global _bridge_started
    if not WHATSAPP_BRIDGE_AUTOSTART:
        return False
    if _bridge_started and _bridge_ready():
        return True

    bridge_js = WHATSAPP_BRIDGE_DIR / "bridge.js"
    if not bridge_js.exists():
        log.error(f"whatsapp: bridge not found at {bridge_js} — run its npm install first")
        return False
    if not (WHATSAPP_BRIDGE_DIR / ".wwebjs_auth").exists():
        log.error("whatsapp: bridge has no saved session — run `npm start` once and "
                  "scan the QR before relying on autostart")
        return False

    log.info("whatsapp: bridge not ready — launching it headless...")
    try:
        flags = 0
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):  # Windows: fully detach
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)
        log_path = WHATSAPP_BRIDGE_DIR / "bridge.log"
        with open(log_path, "a", encoding="utf-8") as logf:
            subprocess.Popen(
                ["node", "bridge.js"],
                cwd=str(WHATSAPP_BRIDGE_DIR),
                stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
        _bridge_started = True
    except FileNotFoundError:
        log.error("whatsapp: `node` not found on PATH — install Node.js to use the bridge")
        return False
    except Exception as exc:
        log.error(f"whatsapp: failed to launch bridge — {exc}")
        return False

    # Poll until the WhatsApp client authenticates (Chromium boot + session load).
    deadline = time.time() + WHATSAPP_BRIDGE_READY_TIMEOUT
    while time.time() < deadline:
        if _bridge_ready():
            log.info("whatsapp: bridge is ready")
            return True
        time.sleep(2)
    log.error(f"whatsapp: bridge did not become ready within "
              f"{WHATSAPP_BRIDGE_READY_TIMEOUT}s (check {WHATSAPP_BRIDGE_DIR / 'bridge.log'})")
    return False


def _send_via_bridge(target: str, message: str) -> bool:
    """
    Send through the headless Node bridge. Returns True only on a confirmed send
    (the bridge resolves the WhatsApp id and reports the server-side message id).
    Raises on any failure so the retry loop in send_whatsapp can react.
    """
    if not _bridge_ready() and not _autostart_bridge():
        raise RuntimeError(
            "bridge unreachable / not logged in — start it with `npm start` in "
            f"{WHATSAPP_BRIDGE_DIR} and scan the QR once")
    r = requests.post(
        f"{WHATSAPP_BRIDGE_URL}/send",
        json={"phone": target, "message": message},
        headers=_bridge_headers(),
        timeout=90,
    )
    if r.ok and r.json().get("ok"):
        return True
    detail = ""
    try:
        detail = r.json().get("error", "")
    except Exception:
        detail = r.text[:200]
    raise RuntimeError(f"bridge send failed (HTTP {r.status_code}): {detail}")


def _send_via_pywhatkit(target: str, message: str) -> None:
    """Legacy GUI-automation send. Imported lazily so the bridge path needs no
    pywhatkit install. Note: silently fails when the screen is off / locked."""
    import pywhatkit  # noqa: PLC0415 — optional dependency, only the legacy path needs it
    pywhatkit.sendwhatmsg_instantly(
        phone_no=target,
        message=message,
        wait_time=WAIT_TIME,
        tab_close=True,
        close_time=CLOSE_TIME,
    )


def _bridge_state(timeout: float = 5.0) -> str:
    """Reported bridge state: 'ready' | 'qr' | 'starting' | 'auth_failure' |
    'disconnected' | 'unreachable'."""
    try:
        r = requests.get(f"{WHATSAPP_BRIDGE_URL}/status",
                         headers=_bridge_headers(), timeout=timeout)
        if r.ok:
            j = r.json()
            return "ready" if j.get("ready") else str(j.get("state", "not_ready"))
    except Exception:
        pass
    return "unreachable"


def bridge_health(timeout: float = 2.0) -> dict:
    """
    Lightweight, read-only health probe for the dashboard / status checks.
    Does NOT autostart anything (safe to poll). Returns:
      {backend, ready, state, configured}
    For non-bridge backends, reports ready=True (nothing to monitor).
    """
    if WHATSAPP_BACKEND != "bridge":
        return {"backend": WHATSAPP_BACKEND, "ready": True,
                "state": "n/a", "configured": bool(WHATSAPP_PHONE or WHATSAPP_PHONES)}
    state = _bridge_state(timeout=timeout)
    return {"backend": "bridge", "ready": state == "ready", "state": state,
            "configured": bool(WHATSAPP_PHONE or WHATSAPP_PHONES)}


def ensure_bridge_ready() -> bool:
    """
    Startup self-check for the headless WhatsApp bridge.

    Intended to be called by the scheduler at launch and at the top of each job.
    When WHATSAPP_BACKEND == 'bridge':
      • returns True (logs OK) if the bridge is logged in and ready;
      • otherwise launches it headless (autostart) and waits for it to reconnect
        from its saved session;
      • if it still isn't ready, logs a loud, actionable WARNING and returns
        False — so a never-scanned or expired session is surfaced at 8 AM instead
        of failing silently when the first alert tries to send.

    No-op (returns True) for any other backend.
    """
    if WHATSAPP_BACKEND != "bridge":
        log.info(f"whatsapp self-check: backend={WHATSAPP_BACKEND} (no bridge needed)")
        return True

    if _bridge_ready() or _autostart_bridge():
        log.info("whatsapp self-check: bridge is logged in and READY")
        return True

    # Not ready — build an actionable reason for the warning.
    state       = _bridge_state()
    auth_exists = (WHATSAPP_BRIDGE_DIR / ".wwebjs_auth").exists()
    if not auth_exists:
        hint = (f"never linked. One-time step: run `npm start` in {WHATSAPP_BRIDGE_DIR} "
                "and scan the QR (WhatsApp > Linked Devices). After that the scheduler "
                "starts it headlessly — no terminal needed.")
    elif state in ("qr", "auth_failure"):
        hint = (f"the linked session expired. Re-run `npm start` in {WHATSAPP_BRIDGE_DIR} "
                "once and re-scan the QR.")
    else:
        hint = (f"state={state}. Check Node.js is on PATH and see "
                f"{WHATSAPP_BRIDGE_DIR / 'bridge.log'}.")
    log.warning(f"whatsapp self-check: WhatsApp bridge is NOT ready — alerts may not be "
                f"delivered. {hint}")
    return False


# ── Core sender ───────────────────────────────────────────────────────────────

def send_whatsapp(message: str, phone: Optional[str] = None, retries: int = 1) -> bool:
    """
    Send a single message via the configured backend (config.WHATSAPP_BACKEND).
    Never raises. Returns True only on a confirmed send.

    "bridge"  — headless; works with the screen off / device locked (recommended).
    "pywhatkit" — GUI automation; only works on an unlocked, focused desktop.

    Retries on failure: transient page-load / readiness hiccups usually clear on
    a second attempt.
    """
    target = phone or WHATSAPP_PHONE
    if not target:
        log.warning("whatsapp: WHATSAPP_PHONE not configured — skipping")
        return False
    if len(message) > MAX_CHARS:
        message = message[:MAX_CHARS - 3] + "..."

    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        try:
            if WHATSAPP_BACKEND == "pywhatkit":
                _send_via_pywhatkit(target, message)
            else:
                _send_via_bridge(target, message)
            suffix = "" if attempt == 1 else f" (succeeded on attempt {attempt}/{attempts})"
            log.info(f"whatsapp: message sent OK via {WHATSAPP_BACKEND}{suffix}")
            return True
        except Exception as exc:
            if attempt < attempts:
                log.warning(f"whatsapp: send attempt {attempt}/{attempts} failed — retrying — {exc}")
                time.sleep(5 if WHATSAPP_BACKEND != "pywhatkit" else WAIT_TIME)
            else:
                log.error(f"whatsapp: send FAILED after {attempts} attempt(s) — {exc}")
    return False


# ── Public alert builders ─────────────────────────────────────────────────────

def send_batch_signal_alert(signals: list[dict], run_date: str) -> bool:
    """
    Build and send investor-readable WhatsApp alerts for stocks whose
    conviction-weighted NET (after-cost) expected return clears MIN_AVG_RETURN.

    The message is written for an investor, not a quant. Per stock:
      *STOCK*  —  BUY 📈
      Conviction ●●●○○ (3/5)
      Expected: +0.8% over ~7 trading days (after costs)
      Track record: won 63% of 175 trades (worst case ≥ 58%)
      Risk: avg loss −1.1% when wrong · stopped out 19% of the time
      Yesterday 05 Jun: ...
      Why N strateg(ies) flagged this:
       • *Strong-Trend Re-Entry* — plain-English reason. (won 60% · +0.5%/3d)

    All figures are net of an assumed round-trip transaction cost (see
    config.TRANSACTION_COST). Messages over MAX_CHARS are split and sent in order.
    """
    # Human-readable description of the active screens — reused in logs and the
    # 'no setups' note so both reflect the .env-configured thresholds.
    screen_desc = f"net return ≥ {MIN_AVG_RETURN:.2%}"
    if MIN_CONFIDENCE > 0:
        screen_desc += f" & win rate ≥ {MIN_CONFIDENCE:.0%}"

    if not signals:
        log.info("whatsapp: no setups fired — sending 'no setups' note")
        return send_no_setups_alert(run_date, screen_desc)

    stats   = _load_stats()
    weights = _load_weights()

    # ── 1. Sort stocks by conviction, highest first ───────────────────────────
    ranked = rank_by_conviction(signals)

    # Date of the most recent OHLCV bar the signals were evaluated against —
    # can lag run_date (pipeline execution date) over weekends/holidays/delays.
    data_date = max((s.get("date", "") for s in signals if s.get("date")), default="")

    # ── 2. Keep stocks that clear BOTH the net avg-return AND confidence gates ─
    qualifying: list[tuple] = []
    for symbol, sigs, dominant, score in ranked:
        net_ret  = _stock_avg_return(sigs, stats, weights)
        net_conf = _stock_confidence(sigs, stats, weights)
        net_wrlo = _stock_wr_lower(sigs, stats, weights)
        net_loss = _stock_avg_loss(sigs, stats, weights)
        net_sl   = _stock_sl_rate(sigs, stats, weights)
        if net_ret >= MIN_AVG_RETURN and net_conf >= MIN_CONFIDENCE:
            qualifying.append((symbol, sigs, dominant, score,
                               net_ret, net_conf, net_wrlo, net_loss, net_sl))

    if not qualifying:
        log.info(f"whatsapp: 0/{len(ranked)} stocks cleared screen ({screen_desc}) "
                 f"— sending 'no setups' note")
        return send_no_setups_alert(run_date, screen_desc)

    log.info(f"whatsapp: {len(qualifying)}/{len(ranked)} stocks cleared screen ({screen_desc})")

    # Log the realised-outcome tracking rows for the picks we're about to send.
    _record_pick_outcomes(qualifying, stats, weights, data_date)

    n_sigs   = sum(len(sigs) for t in qualifying for sigs in (t[1],))
    n_stocks = len(qualifying)

    def _setup_line(setup_name: str, direction: str) -> str:
        """
        One investor-readable bullet for a single strategy, e.g.:
          • *Strong-Trend Re-Entry* — A strong trend dipped to its 20-day
            average... (won 60% of 80 · +0.5% over 3d)
        Uses long stats for buy/neutral, short stats for sell.
        """
        why  = _plain_why(setup_name)
        name = _plain_name(setup_name)
        info = _dir_stats(setup_name, direction, stats) if setup_name in stats else {}
        best_n = info.get("sample_size", 0)

        track = ""
        if best_n:
            best_conf = info.get("best_confidence", 0.5)
            best_avg  = info.get("best_avg_return", 0.0)
            best_d    = info.get("best_days", 1)
            horizon   = "same day" if best_d <= 1 else f"{best_d}d"
            track = f"  _(won {best_conf:.0%} of {best_n} · {best_avg:+.1%} over {horizon})_"

        line = f"• *{name}*"
        if why:
            line += f" — {why}"
        return line + track

    def _build(slice_: list[tuple], part: int, total: int) -> str:
        hdr = [f"*Trade Setups — {run_date}*"]
        if data_date:
            hdr.append(f"_Based on prices through {data_date}_")
        hdr.append(f"_{n_stocks} stock(s) cleared our after-cost screen_")
        if total > 1:
            hdr.append(f"_(Part {part} of {total})_")
        hdr.append("")

        body: list[str] = []
        for (symbol, sigs, dominant, score,
             net_ret, net_conf, net_wrlo, net_loss, net_sl) in slice_:
            display = symbol.replace(".NS", "").replace(".BO", "")
            arrow   = "📈" if dominant == "BUY" else "📉"
            stars   = _conviction_stars(net_wrlo, net_ret)
            n_set   = len(sigs)

            body.append("━━━━━━━━━━━━━━━━━━━━")
            body.append(f"*{display}*  —  {dominant} {arrow}")
            body.append(f"Conviction {stars}")
            body.append(f"📊 Expected: {net_ret:+.1%} per trade (after costs)")
            if net_conf:
                wr_txt = f"won {net_conf:.0%} of past trades"
                if net_wrlo:
                    wr_txt += f" (worst case ≥ {net_wrlo:.0%})"
                body.append(f"✅ Track record: {wr_txt}")
            risk_bits = []
            if net_loss < 0:
                risk_bits.append(f"avg loss {net_loss:+.1%} when wrong")
            if net_sl > 0:
                risk_bits.append(f"stopped out {net_sl:.0%} of the time")
            if risk_bits:
                body.append("⚠️ Risk: " + " · ".join(risk_bits))
            ohlc = _ohlc_line(symbol)
            if ohlc:
                body.append(ohlc)
            body.append("")

            def _sig_sort_key(s: dict) -> int:
                d = _direction_of(s)
                if (dominant == "BUY"  and d == "buy") or \
                   (dominant == "SELL" and d == "sell"):
                    return 0
                if d == "neutral":
                    return 1
                return 2

            label = "strategy agrees" if n_set == 1 else "strategies agree"
            body.append(f"_Why ({n_set} {label}):_")
            for sig in sorted(sigs, key=_sig_sort_key):
                setup         = sig["setup_name"]
                sig_direction = _direction_of(sig)
                body.append(_setup_line(setup, sig_direction))
            body.append("")

        foot = [
            "_Model-based signals, net of estimated costs. Past performance "
            "is not a guarantee. Size positions and use stops responsibly._"
        ]
        return "\n".join(hdr + body + foot)

    # Try single message first
    full = _build(qualifying, 1, 1)
    if len(full) <= MAX_CHARS:
        return send_whatsapp(full)

    # Split into chunks of 2 stocks each
    chunk_size = 2
    chunks     = [qualifying[i:i + chunk_size]
                  for i in range(0, n_stocks, chunk_size)]
    total      = len(chunks)

    ok = True
    for i, chunk in enumerate(chunks, start=1):
        ok = send_whatsapp(_build(chunk, i, total)) and ok
        if i < total:
            time.sleep(WAIT_TIME + CLOSE_TIME + 3)
    return ok


def send_news_picks_alert(messages: list[str]) -> bool:
    """
    Send news-based AI stock picks via WhatsApp — main picks AND every scout
    lens (Hidden Gems / Small-Cap Growth / Smart Money) all route through here.

    `messages` is a list of pre-formatted strings (already split to fit
    MAX_CHARS) produced by news_analyzer.formatter.format_messages() /
    format_scout_messages().

    Sent to every recipient configured in WHATSAPP_PHONES (.env — comma
    separated; falls back to the single WHATSAPP_PHONE). Returns True only
    if every part was confirmed sent to every recipient.
    """
    if not messages:
        return True

    targets = [p for p in WHATSAPP_PHONES if p]
    if not targets:
        log.warning("whatsapp: no recipient phone number(s) configured "
                    "(WHATSAPP_PHONES / WHATSAPP_PHONE) — skipping")
        return False

    total  = len(messages) * len(targets)
    failed = 0
    n = 0
    for phone in targets:
        for msg in messages:
            n += 1
            if not send_whatsapp(msg, phone=phone):
                failed += 1
            if n < total:
                time.sleep(WAIT_TIME + CLOSE_TIME + 3)

    if failed:
        log.error(f"whatsapp: {failed}/{total} send(s) FAILED "
                  f"across {len(targets)} recipient(s)")
    else:
        log.info(f"whatsapp: all {total} send(s) confirmed "
                 f"across {len(targets)} recipient(s)")
    return failed == 0


def send_analysis_started_alert(kind: str, run_date: str,
                                phones: Optional[list[str]] = None) -> bool:
    """
    Heads-up that a morning analysis run has STARTED, sent before the scans begin.

    Doubles as a 'did the scheduled job actually fire?' heartbeat and warms up the
    headless bridge so the picks that follow send without a cold-start delay.

    `phones` is the recipient list — pass WHATSAPP_PHONES for the news run (so the
    heads-up reaches the same audience as the picks); defaults to the single owner
    number (WHATSAPP_PHONE), matching the technical signal alerts.
    """
    msg = (f"*Started {kind} for {run_date}*\n"
           f"_Scanning now — results will follow shortly._")
    targets = [p for p in (phones if phones is not None else [WHATSAPP_PHONE]) if p]
    if not targets:
        log.warning("whatsapp: no recipient configured for started-alert — skipping")
        return False
    ok = True
    for i, phone in enumerate(targets):
        ok = send_whatsapp(msg, phone=phone) and ok
        if i < len(targets) - 1:
            time.sleep(WAIT_TIME + CLOSE_TIME + 3)
    return ok


def send_no_setups_alert(run_date: str, screen_desc: str = "") -> bool:
    """
    Tell the owner that no stocks cleared today's screen, so a quiet morning
    reads as 'scanned, nothing qualified' rather than 'did it even run?'.
    Goes to WHATSAPP_PHONE (same audience as the technical signal alerts).
    """
    extra = f" ({screen_desc})" if screen_desc else ""
    msg = (f"*Trade Setups — {run_date}*\n"
           f"_No stocks cleared today's screen{extra}. "
           f"No new positions today._")
    return send_whatsapp(msg)


def _record_pick_outcomes(qualifying: list[tuple], stats: dict, weights: dict,
                          signal_date: str) -> None:
    """
    Log each sent pick into the outcome tracker (entry = next session's open;
    realised return filled in by later pipeline runs). Never blocks the alert.
    """
    if not signal_date:
        return
    try:
        from analytics import outcomes
        picks = []
        for (symbol, sigs, dominant, score,
             net_ret, net_conf, net_wrlo, net_loss, net_sl) in qualifying:
            horizon = max(1, round(_stock_metric(sigs, stats, weights, "best_days", 1)))
            setup_names = ",".join(sorted({s["setup_name"] for s in sigs}))
            picks.append({
                "symbol"         : symbol,
                "direction"      : dominant,
                "horizon_days"   : horizon,
                "expected_return": net_ret,
                "expected_conf"  : net_conf,
                "n_setups"       : len(sigs),
                "setups"         : setup_names,
            })
        outcomes.record_picks(picks, signal_date)
    except Exception as exc:
        log.warning(f"whatsapp: outcome recording skipped — {exc}")


def send_ingestion_failure_alert(failed_symbols: list[str], run_date: str) -> bool:
    """Alert that some symbols failed data ingestion."""
    sym_list = ", ".join(failed_symbols[:30])
    extra    = f" (+{len(failed_symbols) - 30} more)" if len(failed_symbols) > 30 else ""
    msg = (
        f"*Ingestion Failures — {run_date}*\n"
        f"_{len(failed_symbols)} symbol(s) failed to download_\n\n"
        f"{sym_list}{extra}\n\n"
        f"Check logs and manually verify missing data."
    )
    return send_whatsapp(msg)


# ── CLI: manual bridge self-check ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    print(f"WhatsApp backend: {WHATSAPP_BACKEND}")
    if WHATSAPP_BACKEND == "bridge":
        print(f"Bridge URL:       {WHATSAPP_BRIDGE_URL}")
        print(f"Reported state:   {_bridge_state()}")
    ok = ensure_bridge_ready()
    print("Self-check:", "READY ✅" if ok else "NOT READY ❌ (see warning above)")
    sys.exit(0 if ok else 1)
