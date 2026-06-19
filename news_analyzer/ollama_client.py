"""
Lightweight Ollama HTTP client (no ollama package required — uses requests).

Connects to a locally running Ollama server (default http://localhost:11434).
Automatically selects the best available model — prefers gemma (esp. QAT
variants for their low VRAM-to-quality ratio), then qwen, llama, mistral —
scored by family + parameter size; falls back to any model.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Any

import requests

from utils.logger import get_logger

log = get_logger("ollama_client")


def _host() -> str:
    from config import OLLAMA_HOST
    return OLLAMA_HOST.rstrip("/")


# ── Model selection ───────────────────────────────────────────────────────────

_MODEL_CACHE: str | None = None


def _score_model(name: str) -> int:
    """
    Prefer gemma > qwen > llama > mistral; larger parameter counts score higher;
    QAT (quantization-aware-trained) variants get a bonus — they retain near-fp
    quality at a fraction of the VRAM (e.g. gemma4:12b-it-qat ≈ 5.2GB loaded vs
    qwen3:8b ≈ 5.9GB, despite having 50% more parameters — confirmed empirically).
    """
    n = name.lower()
    if not any(fam in n for fam in ("gemma", "qwen", "llama", "mistral")):
        return 0
    score = 1
    for fam, pts in [("gemma", 15), ("qwen", 10), ("llama", 8), ("mistral", 6)]:
        if fam in n:
            score += pts
            break
    if "qat" in n:
        score += 8
    # Prefer larger parameter counts
    for size, pts in [("72b", 50), ("32b", 40), ("14b", 30), ("12b", 28),
                      ("8b", 22), ("7b", 20), ("4b", 10), ("3b", 8),
                      ("1.5b", 5), ("0.5b", 1)]:
        if size in n:
            score += pts
            break
    return score


def available_model() -> str | None:
    """Return the model to use from the local Ollama instance.

    An explicit config.OLLAMA_MODEL (env OLLAMA_MODEL) wins when that model is
    installed — matched exactly, by family (before ':'), or by prefix, so
    'gemma4:31b-it-qat' or just 'gemma4:31b' both resolve. Otherwise the best
    model is auto-selected by _score_model.
    """
    global _MODEL_CACHE
    if _MODEL_CACHE:
        return _MODEL_CACHE
    try:
        from config import OLLAMA_MODEL
    except Exception:
        OLLAMA_MODEL = ""
    try:
        r = requests.get(f"{_host()}/api/tags", timeout=5)
        r.raise_for_status()
        models = r.json().get("models", [])
        if not models:
            return None
        names = [m.get("name", "") for m in models]

        if OLLAMA_MODEL:
            want = OLLAMA_MODEL.strip()
            match = next((n for n in names
                          if n == want or n.split(":")[0] == want or n.startswith(want)), None)
            if match:
                _MODEL_CACHE = match
                log.info(f"ollama: using configured model '{match}' (OLLAMA_MODEL)")
                return _MODEL_CACHE
            log.warning(f"ollama: configured OLLAMA_MODEL '{want}' is not installed "
                        f"(have: {names}) — falling back to auto-select")

        scored = sorted(models, key=lambda m: _score_model(m.get("name", "")), reverse=True)
        _MODEL_CACHE = scored[0]["name"]
        log.info(f"ollama: selected model '{_MODEL_CACHE}' from {len(models)} available")
        return _MODEL_CACHE
    except Exception as exc:
        log.warning(f"ollama: cannot list models — {exc}")
        return None


def is_available() -> bool:
    """Return True if Ollama is reachable and has at least one model."""
    return available_model() is not None


def _start_server() -> bool:
    """
    Launch `ollama serve` as a background process and wait up to 30s for it
    to become reachable.  Returns True on success, False otherwise.
    Only called when the server is not already running.
    """
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # no console popup

    try:
        subprocess.Popen(["ollama", "serve"], **kwargs)
        log.info("ollama: launched 'ollama serve' — waiting for readiness (up to 30s)...")
    except FileNotFoundError:
        log.error("ollama: 'ollama' executable not found — install Ollama from https://ollama.com")
        return False
    except Exception as exc:
        log.warning(f"ollama: could not start server — {exc}")
        return False

    for attempt in range(15):
        time.sleep(2)
        try:
            r = requests.get(f"{_host()}/api/tags", timeout=3)
            if r.status_code == 200:
                log.info(f"ollama: server ready (after {(attempt + 1) * 2}s)")
                return True
        except Exception:
            pass

    log.warning("ollama: server did not become ready within 30s")
    return False


def ensure_available() -> bool:
    """
    Return True if Ollama is ready (starting it automatically if needed).
    Use this instead of is_available() in scheduled/unattended contexts.
    """
    global _MODEL_CACHE
    if is_available():
        return True

    log.info("ollama: server not reachable — attempting auto-start")
    _MODEL_CACHE = None  # reset cache so available_model() re-queries after start
    if not _start_server():
        return False

    # Re-probe model list after server comes up
    _MODEL_CACHE = None
    return is_available()


# ── Server diagnostics ───────────────────────────────────────────────────────

def server_status() -> None:
    """
    Log a snapshot of Ollama server state:
    - Available models (from /api/tags)
    - Currently loaded models + VRAM usage (from /api/ps)
    """
    host = _host()
    # Available models
    try:
        r = requests.get(f"{host}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m.get("name", "?") for m in r.json().get("models", [])]
        log.info(f"ollama status — available models: {models or '(none)'}")
    except Exception as exc:
        log.warning(f"ollama status — /api/tags failed: {exc}")
        return

    # Loaded models + VRAM
    try:
        r = requests.get(f"{host}/api/ps", timeout=5)
        r.raise_for_status()
        loaded = r.json().get("models", [])
        if not loaded:
            log.info("ollama status — no model currently loaded in VRAM")
        for m in loaded:
            name      = m.get("name", "?")
            vram_mb   = m.get("size_vram", 0) // (1024 * 1024)
            total_mb  = m.get("size", 0) // (1024 * 1024)
            expires   = m.get("expires_at", "?")
            log.info(f"ollama status — loaded: {name}  VRAM {vram_mb} MB / total {total_mb} MB  expires {expires}")
    except Exception as exc:
        log.warning(f"ollama status — /api/ps failed: {exc}")


def warmup_model(model: str | None = None) -> bool:
    """
    Send a minimal prompt to force the model to load into VRAM before the
    real inference call.  Returns True if the model responded successfully.
    Logs VRAM usage after loading.
    """
    m = model or available_model()
    if not m:
        log.warning("ollama warmup — no model selected, skipping")
        return False

    from config import OLLAMA_GEN_TIMEOUT
    # Big models (e.g. 15-20 GB MoE/dense) take a while to cold-load from disk into
    # VRAM/RAM; the probe returns as soon as the model is resident, so give it a
    # generous budget. Too short a timeout returns before the load finishes and the
    # first real generate() then races the still-loading model (and times out).
    warm_timeout = max(300, OLLAMA_GEN_TIMEOUT)
    log.info(f"ollama warmup — loading '{m}' into VRAM (sending 1-token probe)...")
    t0 = time.time()
    try:
        r = requests.post(
            f"{_host()}/api/generate",
            json={"model": m, "prompt": "Hi", "stream": False,
                  "think": False, "options": {"num_predict": 1}},
            timeout=warm_timeout,
        )
        r.raise_for_status()
        elapsed = time.time() - t0
        log.info(f"ollama warmup — model ready in {elapsed:.1f}s")
    except requests.Timeout:
        log.warning(f"ollama warmup — timed out after {warm_timeout}s; model may still be loading")
        return False
    except Exception as exc:
        log.warning(f"ollama warmup — failed: {exc}")
        return False

    # Log VRAM now that the model is loaded
    try:
        ps = requests.get(f"{_host()}/api/ps", timeout=5).json().get("models", [])
        for entry in ps:
            if m in entry.get("name", ""):
                vram_mb = entry.get("size_vram", 0) // (1024 * 1024)
                log.info(f"ollama warmup — '{m}' occupies {vram_mb} MB VRAM")
    except Exception:
        pass

    return True


# ── Inference ─────────────────────────────────────────────────────────────────

def generate(prompt: str, model: str | None = None, timeout: int = 180,
             think: bool = False, num_predict: int | None = None) -> str:
    """
    Call Ollama /api/generate (non-streaming).

    think=False (default): disables chain-of-thought. Use for structured
      output tasks (PICK 1: SYMBOL | ...) where the response must be parseable.
    think=True: enables chain-of-thought reasoning. Use for free-form analysis
      where quality matters more than exact format.

    num_predict: token budget for the response. Defaults to 1024 for
      think=False (structured picks are short) and 3072 for think=True —
      reasoning models spend part of the budget on a hidden "thinking" trace
      before emitting the final answer, and 1024 was observed truncating
      mid-thought, leaving `response` empty (see the warning below).

    Timeout handling: when think=True, a read timeout is RETRIED ONCE (a single
    reasoning call can occasionally spiral into a long hidden 'thinking' trace
    that overruns the deadline; a fresh attempt usually completes). If the retry
    also times out, RuntimeError is raised so the caller can fall back to a
    think=False call (which skips the thinking step and answers directly).
    think=False is not retried.
    """
    m = model or available_model()
    if not m:
        raise RuntimeError("No Ollama model available")

    if num_predict is None:
        num_predict = 3072 if think else 1024

    payload: dict[str, Any] = {
        "model"  : m,
        "prompt" : prompt,
        "stream" : False,
        "think"  : think,
        "options": {
            "temperature": 0.1,
            "num_predict": num_predict,
        },
    }

    def _attempt() -> str:
        log.info(f"ollama generate — model={m}  think={think}  prompt={len(prompt)} chars  "
                 f"num_predict={num_predict}  timeout={timeout}s")
        t0 = time.time()
        # (connect_timeout=15s, read_timeout=Ns) — enforces read deadline independently
        r = requests.post(
            f"{_host()}/api/generate",
            json=payload,
            timeout=(15, timeout),
        )
        r.raise_for_status()
        data   = r.json()
        result = data.get("response", "").strip()
        elapsed = time.time() - t0

        if not result:
            thinking = data.get("thinking", "")
            if thinking:
                log.warning(
                    f"ollama generate — empty final response after {elapsed:.1f}s "
                    f"(think={think}, num_predict={num_predict}); model spent its "
                    f"entire token budget on a {len(thinking)}-char hidden 'thinking' "
                    f"trace and never reached the answer — raise num_predict or "
                    f"call with think=False."
                )
            else:
                log.warning(
                    f"ollama generate — empty response after {elapsed:.1f}s; "
                    f"response keys: {list(data.keys())}"
                )

        log.info(f"ollama generate — done in {elapsed:.1f}s  response={len(result)} chars")
        return result

    # think=True gets one extra attempt on a read timeout; think=False gets none.
    attempts = 2 if think else 1
    for attempt in range(1, attempts + 1):
        try:
            return _attempt()
        except requests.Timeout:
            if think and attempt < attempts:
                log.warning(
                    f"ollama generate — think=True timed out after {timeout}s "
                    f"(attempt {attempt}/{attempts}) — retrying once before the "
                    f"caller falls back to think=False"
                )
                continue
            raise RuntimeError(
                f"Ollama timed out after {timeout}s "
                f"({'x2 ' if think else ''}prompt was {len(prompt)} chars)"
            )
        except Exception as exc:
            raise RuntimeError(f"Ollama generate failed: {exc}") from exc


def unload_model(model: str | None = None) -> None:
    """
    Unload the model from VRAM by setting keep_alive=0.
    Call this after all inference is done to release GPU memory.
    """
    m = model or _MODEL_CACHE
    if not m:
        log.debug("ollama: no model to unload")
        return
    try:
        requests.post(
            f"{_host()}/api/generate",
            json={"model": m, "keep_alive": 0},
            timeout=10,
        )
        log.info(f"ollama: model '{m}' unloaded from VRAM")
    except Exception as exc:
        log.debug(f"ollama: unload request failed — {exc}")


def chat(messages: list[dict], model: str | None = None, timeout: int = 180) -> str:
    """
    Call Ollama /api/chat (non-streaming).
    messages: [{"role": "user"|"system"|"assistant", "content": str}]
    """
    m = model or available_model()
    if not m:
        raise RuntimeError("No Ollama model available")

    payload: dict[str, Any] = {
        "model"   : m,
        "messages": messages,
        "stream"  : False,
        "think"   : False,
        "options" : {"temperature": 0.1, "num_predict": 1024},
    }
    try:
        r = requests.post(
            f"{_host()}/api/chat",
            json=payload,
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "").strip()
    except requests.Timeout:
        raise RuntimeError(f"Ollama chat timed out after {timeout}s")
    except Exception as exc:
        raise RuntimeError(f"Ollama chat failed: {exc}") from exc
