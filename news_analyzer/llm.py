"""
LLM provider facade — one interface, two backends, chosen by config.LLM_PROVIDER.

  "claude" (default) — Claude via the Claude Code CLI in headless print mode
                       (`claude -p --model sonnet`). It authenticates with the
                       Claude subscription already logged in on this machine
                       (~/.claude/.credentials.json), so there is NO API key and
                       no per-token bill — calls count against the plan's normal
                       usage limits. ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are
                       stripped from the child env so the CLI can never silently
                       switch to API billing.
  "ollama"           — the local Ollama model (news_analyzer.ollama_client).

The public surface mirrors ollama_client (generate / chat / ensure_available /
available_model / warmup_model / unload_model / server_status) so callers just
import from here. Extra: generate(..., web=True) lets Claude use its WebSearch +
WebFetch tools (used by the litigation tracker); Ollama ignores it.

If a Claude call fails (usage limit, logged out, network) and LLM_FALLBACK=true,
the same prompt is retried on Ollama so a scheduled run still completes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from news_analyzer import ollama_client as _oc
from utils.logger import get_logger

log = get_logger("llm")

_SYSTEM_PROMPT = (
    "You are a senior Indian equity and legal-risk analyst working inside an "
    "automated pipeline. Your reply is parsed by a program: follow the requested "
    "output format exactly, with no preamble, no closing remarks and no markdown "
    "code fences unless asked. Never invent facts, case numbers or dates; say "
    "'unknown' when you do not know."
)


def provider() -> str:
    from config import LLM_PROVIDER
    return "ollama" if LLM_PROVIDER == "ollama" else "claude"


# ── Claude CLI discovery ──────────────────────────────────────────────────────

_CLI_CACHE: str | None = None
_CLI_LOCK = threading.Lock()


def _version_key(p: Path) -> tuple:
    m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", str(p))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def find_cli() -> str | None:
    """Locate the claude executable (config override → PATH → common installs →
    newest VS Code / Cursor extension bundle). Cached per process."""
    global _CLI_CACHE
    if _CLI_CACHE and Path(_CLI_CACHE).exists():
        return _CLI_CACHE
    from config import CLAUDE_CLI
    home = Path.home()
    cands: list[Path] = []
    if CLAUDE_CLI:
        cands.append(Path(CLAUDE_CLI))
    w = shutil.which("claude")
    if w:
        cands.append(Path(w))
    exe = "claude.exe" if sys.platform == "win32" else "claude"
    cands += [home / ".local" / "bin" / exe, home / ".claude" / "local" / exe]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            cands.append(Path(appdata) / "npm" / "claude.cmd")
    ext: list[Path] = []
    for root in (home / ".vscode" / "extensions", home / ".cursor" / "extensions",
                 home / ".vscode-insiders" / "extensions"):
        if root.exists():
            ext += list(root.glob(f"anthropic.claude-code-*/resources/native-binary/{exe}"))
    cands += sorted(ext, key=_version_key, reverse=True)
    for c in cands:
        if c and c.exists():
            _CLI_CACHE = str(c)
            return _CLI_CACHE
    return None


def _neutral_cwd() -> str:
    """Run the CLI outside the repo so it doesn't load this project's CLAUDE.md,
    hooks or permission settings into every analysis call."""
    d = Path(tempfile.gettempdir()) / "signal_infomer_llm"
    d.mkdir(exist_ok=True)
    return str(d)


def _child_env() -> dict:
    env = os.environ.copy()
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
              "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_BASE_URL"):
        env.pop(k, None)
    return env


_SEM: threading.Semaphore | None = None


def _sem() -> threading.Semaphore:
    global _SEM
    if _SEM is None:
        from config import CLAUDE_MAX_PARALLEL
        _SEM = threading.Semaphore(max(1, CLAUDE_MAX_PARALLEL))
    return _SEM


def _claude_call(prompt: str, timeout: int, think: bool, web: bool,
                 max_turns: int | None, system: str | None,
                 files: list[str] | None = None) -> str:
    from config import CLAUDE_MODEL
    cli = find_cli()
    if not cli:
        raise RuntimeError("claude CLI not found — install Claude Code or set CLAUDE_CLI")
    cmd = [cli, "-p", "--model", CLAUDE_MODEL, "--output-format", "json",
           "--system-prompt", system or _SYSTEM_PROMPT,
           "--strict-mcp-config", "--no-session-persistence", "--setting-sources", ""]
    tools = []
    if web:
        tools += ["WebSearch", "WebFetch"]
    if files:
        # Let Claude open the user's attached files itself (PDF incl. scanned pages,
        # screenshots, HTML) — read-only, scoped to the files' folders.
        tools += ["Read"]
        for d in sorted({str(Path(f).resolve().parent) for f in files}):
            cmd += ["--add-dir", d]
        prompt += "\n\nFILES TO READ (use the Read tool; for long PDFs read the pages you need):\n" + \
            "\n".join(f"- {Path(f).resolve()}" for f in files)
    if tools:
        cmd += ["--tools", ",".join(tools), "--allowedTools", ",".join(tools),
                "--max-turns", str(max_turns or 12)]
    else:
        cmd += ["--tools", "", "--max-turns", str(max_turns or 1)]
    if not think and not web and not files:
        cmd += ["--effort", "low"]     # structured extraction: answer directly, fast

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    log.info(f"claude -p — model={CLAUDE_MODEL} think={think} web={web} "
             f"prompt={len(prompt)} chars timeout={timeout}s")
    t0 = time.time()
    with _sem():
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout,
                                  cwd=_neutral_cwd(), env=_child_env(), **kwargs)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"claude timed out after {timeout}s")
    out = (proc.stdout or "").strip()
    line = next((ln for ln in reversed(out.splitlines()) if ln.lstrip().startswith("{")), "")
    if not line:
        raise RuntimeError(f"claude exited {proc.returncode} with no JSON — "
                           f"{(proc.stderr or out)[-400:]}")
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"claude returned unparseable JSON — {exc}")
    result = (data.get("result") or "").strip()
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        raise RuntimeError(f"claude error ({data.get('subtype')}, "
                           f"api={data.get('api_error_status')}): {result[:300]}")
    log.info(f"claude -p — done in {time.time() - t0:.1f}s  response={len(result)} chars  "
             f"turns={data.get('num_turns')}")
    return result


# ── Public interface (mirrors ollama_client) ──────────────────────────────────

def label() -> str:
    """Short human label for message footers, e.g. 'Claude Sonnet'."""
    if provider() == "claude":
        from config import CLAUDE_MODEL
        m = CLAUDE_MODEL.lower()
        for fam in ("sonnet", "opus", "haiku", "fable"):
            if fam in m:
                return f"Claude {fam.capitalize()}"
        return f"Claude ({CLAUDE_MODEL})"
    return "Ollama AI"


def available_model() -> str | None:
    if provider() == "claude":
        from config import CLAUDE_MODEL
        return f"claude:{CLAUDE_MODEL}" if find_cli() else None
    return _oc.available_model()


def ensure_available() -> bool:
    if provider() == "claude":
        if find_cli():
            return True
        log.error("llm: claude CLI not found")
        from config import LLM_FALLBACK
        return LLM_FALLBACK and _oc.ensure_available()
    return _oc.ensure_available()


_WARM_OK: bool | None = None


def warmup_model(model: str | None = None) -> bool:
    """Claude: one tiny probe per process to confirm the subscription login works
    (fails fast instead of mid-run). Ollama: load weights into VRAM."""
    global _WARM_OK
    if provider() != "claude":
        return _oc.warmup_model(model)
    if _WARM_OK is not None:
        return _WARM_OK
    try:
        _WARM_OK = "OK" in _claude_call("Reply with exactly: OK", 120, False, False, 1, None)
    except Exception as exc:
        log.warning(f"llm: claude probe failed — {exc}")
        from config import LLM_FALLBACK
        _WARM_OK = bool(LLM_FALLBACK and _oc.ensure_available() and _oc.warmup_model())
    return _WARM_OK


def unload_model(model: str | None = None) -> None:
    if provider() != "claude" or _oc._MODEL_CACHE:
        _oc.unload_model(model)


def server_status() -> None:
    if provider() == "claude":
        log.info(f"llm: provider=claude  model={available_model()}  cli={find_cli()}")
    else:
        _oc.server_status()


def generate(prompt: str, model: str | None = None, timeout: int = 180,
             think: bool = False, num_predict: int | None = None,
             web: bool = False, max_turns: int | None = None,
             system: str | None = None, files: list[str] | None = None) -> str:
    """Generate a completion with the configured provider. Same contract as
    ollama_client.generate (returns stripped text, raises RuntimeError).
    files: local paths Claude may open with its Read tool (Ollama: ignored —
    callers must also inline extracted text for the fallback path)."""
    if provider() == "claude":
        from config import CLAUDE_TIMEOUT, LLM_FALLBACK
        try:
            return _claude_call(prompt, max(timeout, 60) if not (web or files) else CLAUDE_TIMEOUT,
                                think, web, max_turns, system, files)
        except Exception as exc:
            if not LLM_FALLBACK:
                raise RuntimeError(str(exc)) from exc
            log.warning(f"llm: claude failed ({exc}) — falling back to Ollama")
            if not _oc.ensure_available():
                raise RuntimeError(f"claude failed and Ollama unavailable: {exc}") from exc
    return _oc.generate(prompt, model=model, timeout=timeout, think=think,
                        num_predict=num_predict)


def chat(messages: list[dict], model: str | None = None, timeout: int = 180) -> str:
    if provider() == "claude":
        system = next((m["content"] for m in messages if m.get("role") == "system"), None)
        convo = "\n\n".join(f"{m['role'].upper()}: {m['content']}"
                            for m in messages if m.get("role") != "system")
        return generate(convo, timeout=timeout, system=system)
    return _oc.chat(messages, model=model, timeout=timeout)


def status() -> dict:
    """Dashboard health snapshot. Never raises, never calls the model."""
    try:
        from config import CLAUDE_MODEL, LLM_FALLBACK
        if provider() == "claude":
            cli = find_cli()
            creds = (Path.home() / ".claude" / ".credentials.json").exists()
            return {"provider": "claude", "model": CLAUDE_MODEL, "label": label(),
                    "up": bool(cli and creds), "cli": cli or "(not found)",
                    "logged_in": creds, "fallback": LLM_FALLBACK}
        return {"provider": "ollama", "label": label(), "up": _oc.is_available(),
                "model": _oc.available_model() or "(none)", "fallback": False}
    except Exception as exc:
        return {"provider": provider(), "up": False, "detail": str(exc)}


def extract_json(text: str):
    """Pull the first JSON object/array out of an LLM reply (tolerates prose and
    ```json fences). Returns the parsed value or None."""
    if not text:
        return None
    t = re.sub(r"```(?:json)?", "", text)
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    opener = t[start]
    closer = "}" if opener == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None
