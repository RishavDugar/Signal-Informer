"""
Read-side helpers for the dashboard.

Everything here is read-only and reuses the existing backend wherever possible:
  - data.db                 for OHLCV / stocks / ingestion log / raw signals
  - notifications.whatsapp  for conviction ranking + avg-return / confidence
  - backtester              for weights + stats JSON
  - setup_loader            for the live set of loaded setups
  - news_analyzer           for news + scout recommendations
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from config import (
    BASE_DIR, DB_PATH, MIN_AVG_RETURN, OLLAMA_HOST, OLLAMA_MODEL,
    SCHEDULE_HOUR, SCHEDULE_MINUTE, NEWS_SCHEDULE_HOUR, NEWS_SCHEDULE_MINUTE,
    WHATSAPP_PHONE, WHATSAPP_PHONES, WHATSAPP_BACKEND,
    NEWS_TOP_N, NEWS_DEDUP_DAYS, SCOUT_DEDUP_DAYS,
)
from data import db
from utils.logger import get_logger

log = get_logger("webui")

ROOT = Path(__file__).resolve().parent.parent
WEIGHTS_PATH = ROOT / "db" / "strategy_weights.json"
PARAMS_PATH = ROOT / "db" / "optimal_params.json"
LOG_PATH = ROOT / "logs" / "signal_infomer.log"
ENV_PATH = ROOT / ".env"


# ── Status / overview ──────────────────────────────────────────────────────────

def _json_meta(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "exists": True,
            "generated_at": data.get("generated_at"),
            "setups": len(data.get("setups", {})),
        }
    except Exception as exc:
        return {"exists": True, "error": str(exc)}


def status() -> dict:
    out: dict = {}

    # DB integrity + size
    try:
        db.init_db()
        out["db_ok"] = db.integrity_check()
    except Exception as exc:
        out["db_ok"] = False
        out["db_error"] = str(exc)
    out["db_size_mb"] = round(DB_PATH.stat().st_size / 1e6, 2) if DB_PATH.exists() else 0

    # Row counts
    counts = {}
    try:
        with db.get_conn() as conn:
            for tbl in ("stocks", "ohlcv", "setup_signals",
                        "news_recommendations", "scout_recommendations",
                        "ingestion_runs"):
                counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            last_sig = conn.execute(
                "SELECT MAX(signal_date) FROM setup_signals"
            ).fetchone()[0]
            last_ohlcv = conn.execute("SELECT MAX(date) FROM ohlcv").fetchone()[0]
            run = conn.execute(
                "SELECT run_at, status, total_stocks, successful, failed "
                "FROM ingestion_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        out["counts"] = counts
        out["last_signal_date"] = last_sig
        out["last_ohlcv_date"] = last_ohlcv
        out["last_ingestion"] = dict(run) if run else None
    except Exception as exc:
        out["counts_error"] = str(exc)

    out["weights"] = _json_meta(WEIGHTS_PATH)
    out["params"] = _json_meta(PARAMS_PATH)

    # Loaded setups
    try:
        from setup_loader import load_setups
        out["setups_loaded"] = len(load_setups())
    except Exception as exc:
        out["setups_loaded"] = None
        out["setups_error"] = str(exc)

    # Ollama
    out["ollama"] = ollama_status()

    # WhatsApp send backend health (bridge readiness etc.) — read-only probe.
    out["whatsapp"] = whatsapp_status()

    out["config"] = {
        "min_avg_return": MIN_AVG_RETURN,
        "schedule": f"{SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d}",
        "news_schedule": f"{NEWS_SCHEDULE_HOUR:02d}:{NEWS_SCHEDULE_MINUTE:02d}",
        "ollama_model": OLLAMA_MODEL or "(auto)",
        "whatsapp_phone": WHATSAPP_PHONE or "(unset)",
        "whatsapp_phones": WHATSAPP_PHONES,
        "whatsapp_backend": WHATSAPP_BACKEND,
        "news_top_n": NEWS_TOP_N,
        "news_dedup_days": NEWS_DEDUP_DAYS,
        "scout_dedup_days": SCOUT_DEDUP_DAYS,
    }
    return out


def whatsapp_status() -> dict:
    """Send-backend health for the dashboard (bridge readiness). Never raises."""
    try:
        from notifications.whatsapp import bridge_health
        return bridge_health()
    except Exception as exc:
        return {"backend": WHATSAPP_BACKEND, "ready": False,
                "state": f"error: {exc}", "configured": False}


def outcomes_scorecard() -> dict:
    """Realised-vs-expected pick performance over the trailing window. Never raises."""
    try:
        from analytics.outcomes import scorecard
        return scorecard()
    except Exception as exc:
        log.warning(f"webui: outcomes scorecard failed — {exc}")
        return {"n": 0, "pending": 0, "error": str(exc)}


def ollama_status() -> dict:
    try:
        import requests
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=2)
        if r.status_code == 200:
            models = [m.get("name") for m in r.json().get("models", [])]
            return {"up": True, "models": models}
        return {"up": False, "detail": f"HTTP {r.status_code}"}
    except Exception as exc:
        return {"up": False, "detail": str(exc)}


# ── Signals (with conviction ranking) ──────────────────────────────────────────

def _latest_signal_date(conn) -> str | None:
    row = conn.execute("SELECT MAX(signal_date) FROM setup_signals").fetchone()
    return row[0] if row else None


def signals(on_date: str | None = None, top_n: int = 50) -> dict:
    """Return ranked stocks (conviction + avg-return) for a signal date."""
    from notifications import whatsapp as wa

    with db.get_conn() as conn:
        if not on_date:
            on_date = _latest_signal_date(conn)
        if not on_date:
            return {"date": None, "ranked": [], "dates": []}

        rows = conn.execute(
            """
            SELECT s.symbol AS symbol, ss.setup_name, ss.signal_date, ss.signal, ss.metadata
            FROM setup_signals ss
            JOIN stocks s ON s.id = ss.stock_id
            WHERE ss.signal_date = ? AND ss.signal = 1
            """,
            (on_date,),
        ).fetchall()

        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT signal_date FROM setup_signals ORDER BY signal_date DESC LIMIT 30"
        ).fetchall()]

    sig_dicts = []
    for r in rows:
        try:
            meta = json.loads(r["metadata"] or "{}")
        except Exception:
            meta = {}
        sig_dicts.append({
            "symbol": r["symbol"],
            "setup_name": r["setup_name"],
            "date": r["signal_date"],
            "signal": bool(r["signal"]),
            "metadata": meta,
        })

    stats = wa._load_stats()
    weights = wa._load_weights()
    ranked = wa.rank_by_conviction(sig_dicts, top_n=top_n)

    result = []
    for symbol, sigs, dominant, score in ranked:
        net_ret  = wa._stock_avg_return(sigs, stats, weights)
        net_conf = wa._stock_confidence(sigs, stats, weights)
        net_wrlo = wa._stock_wr_lower(sigs, stats, weights)
        net_loss = wa._stock_avg_loss(sigs, stats, weights)
        setups = []
        for sig in sigs:
            direction = wa._direction_of(sig)
            info = wa._dir_stats(sig["setup_name"], direction, stats)
            name = sig["setup_name"]
            setups.append({
                "name": name,
                "friendly": wa._plain_name(name),
                "direction": direction,
                "desc": wa._plain_why(name),
                "avg_return": info.get("best_avg_return"),      # net of costs
                "confidence": info.get("best_confidence"),       # win rate
                "wr_lower": info.get("best_wr_lower"),           # worst-case win rate
                "profit_factor": info.get("profit_factor"),
                "avg_loss": info.get("avg_loss"),
                "sl_rate": info.get("sl_rate"),
                "best_day": info.get("best_days"),
                "sample_size": info.get("sample_size"),
            })
        result.append({
            "symbol": symbol.replace(".NS", "").replace(".BO", ""),
            "raw_symbol": symbol,
            "dominant": dominant,
            "score": score,
            "avg_return": net_ret,      # net of estimated costs
            "confidence": net_conf,     # conviction-weighted win rate
            "wr_lower": net_wrlo,       # worst-case (Wilson lower-bound) win rate
            "avg_loss": net_loss,
            "qualifies": net_ret >= MIN_AVG_RETURN,
            "n_setups": len(sigs),
            "setups": setups,
        })

    return {
        "date": on_date,
        "ranked": result,
        "dates": dates,
        "min_avg_return": MIN_AVG_RETURN,
    }


# ── News + scout recommendations ───────────────────────────────────────────────

def news(limit: int = 60) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT symbol, company_name, rec_date, catalyst, analysis,
                   cmp, change_1d_pct, change_5d_pct, change_20d_pct,
                   whatsapp_sent, sent_at
            FROM news_recommendations
            ORDER BY rec_date DESC, id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def scout(limit: int = 90) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT scout_type, symbol, company_name, rec_date, catalyst,
                   reasoning, analysis, cmp, change_1d_pct, change_5d_pct,
                   change_20d_pct, whatsapp_sent, sent_at
            FROM scout_recommendations
            ORDER BY rec_date DESC, id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Setups catalogue ───────────────────────────────────────────────────────────

def setups() -> list[dict]:
    params_all: dict = {}
    if PARAMS_PATH.exists():
        try:
            params_all = json.loads(PARAMS_PATH.read_text(encoding="utf-8")).get("setups", {})
        except Exception:
            params_all = {}

    weights = {}
    try:
        from backtester import load_directional_weights
        weights = load_directional_weights() or {}
    except Exception:
        pass

    from notifications.whatsapp import _CATALOGUE

    out = []
    try:
        from setup_loader import load_setups
        instances = load_setups()
    except Exception as exc:
        log.warning(f"webui: load_setups failed — {exc}")
        instances = []

    for inst in instances:
        name = getattr(inst, "name", "?")
        p = params_all.get(name, {})
        w = weights.get(name, {})
        out.append({
            "name": name,
            "sl_pct": getattr(inst, "sl_pct", None),
            "params": p.get("params", {}),
            "best_avg_return": p.get("best_avg_return"),
            "best_sl_rate": p.get("best_sl_rate"),
            "best_days": p.get("best_days"),
            "long_weight": (w.get("long") if isinstance(w, dict) else None),
            "short_weight": (w.get("short") if isinstance(w, dict) else None),
            "desc": _CATALOGUE.get(name, ("", "", ""))[0],
        })
    out.sort(key=lambda x: (x["best_avg_return"] or -999), reverse=True)
    return out


# ── Stocks + OHLCV ─────────────────────────────────────────────────────────────

def stocks() -> list[dict]:
    rows = db.get_active_stocks()
    out = []
    with db.get_conn() as conn:
        for s in rows:
            r = conn.execute(
                "SELECT MAX(date) d, COUNT(*) n FROM ohlcv WHERE stock_id = ?",
                (s["id"],),
            ).fetchone()
            out.append({**s, "last_date": r["d"], "rows": r["n"]})
    return out


def ohlcv(symbol: str, days: int = 120) -> dict:
    df = db.get_ohlcv(symbol, days=days)
    if df.empty:
        return {"symbol": symbol, "rows": []}
    rows = [
        {
            "date": idx.strftime("%Y-%m-%d"),
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": int(r["volume"]),
        }
        for idx, r in df.iterrows()
    ]
    return {"symbol": symbol, "rows": rows}


# ── Config (.env) ──────────────────────────────────────────────────────────────

# Keys the UI is allowed to edit, with light typing for the form.
EDITABLE_KEYS = [
    "WHATSAPP_PHONE", "WHATSAPP_PHONES",
    "WHATSAPP_BACKEND", "WHATSAPP_BRIDGE_URL", "WHATSAPP_BRIDGE_AUTOSTART",
    "MIN_AVG_RETURN", "TRANSACTION_COST",
    "NOTIFY_ON_SIGNAL", "NOTIFY_ON_INGESTION_FAILURE",
    "MAX_WORKERS", "RETRY_ATTEMPTS", "HISTORY_DAYS",
    "BACKTEST_WINDOW_DAYS", "MAX_HOLD_DAYS", "BEST_DAY_THRESHOLD",
    "SCHEDULE_HOUR", "SCHEDULE_MINUTE", "SCHEDULE_TZ",
    "NEWS_SCHEDULE_HOUR", "NEWS_SCHEDULE_MINUTE",
    "NEWS_DEDUP_DAYS", "NEWS_TOP_N", "SCOUT_DEDUP_DAYS",
    "OLLAMA_HOST", "OLLAMA_MODEL",
]


def read_env() -> dict:
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    return {
        "keys": EDITABLE_KEYS,
        "values": {k: values.get(k, "") for k in EDITABLE_KEYS},
        "path": str(ENV_PATH),
    }


def write_env(updates: dict[str, str]) -> dict:
    """Update only EDITABLE_KEYS in .env, preserving comments/order/other keys."""
    safe = {k: str(v) for k, v in updates.items() if k in EDITABLE_KEYS}
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen: set[str] = set()
    out_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in safe:
                out_lines.append(f"{key}={safe[key]}")
                seen.add(key)
                continue
        out_lines.append(line)
    # Append any editable keys that weren't already present
    appended = [k for k in safe if k not in seen]
    if appended:
        out_lines.append("")
        out_lines.append("# ── Added by dashboard ──")
        for k in appended:
            out_lines.append(f"{k}={safe[k]}")
    ENV_PATH.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return {"updated": list(safe.keys()), "note": "Restart pipelines/server to apply."}


# ── Logs ───────────────────────────────────────────────────────────────────────

def logs(lines: int = 400) -> dict:
    if not LOG_PATH.exists():
        return {"path": str(LOG_PATH), "lines": []}
    try:
        content = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return {"path": str(LOG_PATH), "lines": [f"[read error] {exc}"]}
    return {"path": str(LOG_PATH), "lines": content[-lines:]}
