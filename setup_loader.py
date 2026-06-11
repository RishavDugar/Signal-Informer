"""
Dynamic loader for the "Trading Setups" folder.

Convention:
  - Any *.py file in Trading Setups/ (except __init__.py / base_setup.py) is scanned
  - Every class that subclasses BaseSetup (directly or indirectly) and has a non-empty
    `name` attribute is registered
  - Instantiated with default constructor (no args) unless constructor is customised

Usage:
    from setup_loader import load_setups
    setups = load_setups()          # list[BaseSetup]
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path

from core.base_setup import BaseSetup
from utils.logger import get_logger

log = get_logger(__name__)

_SETUP_DIR          = Path(__file__).parent / "Trading Setups"
_OPTIMAL_PARAMS_PATH = Path(__file__).parent / "db" / "optimal_params.json"

# Discovery is expensive (re-execs every setup module) and noisy in the logs, so
# memoize per process keyed by the params flag. The long-running web UI polls
# /api/status every few seconds — without this it would re-scan + re-log on every
# poll. Callers treat the returned setups as read-only (see backtester/pipeline).
_CACHE: dict[bool, list[BaseSetup]] = {}


def _load_optimal_params() -> dict[str, dict]:
    """
    Load best hyperparameters from the last hyperparameter_search.py run.
    Returns {setup_name: {param: value}} — empty if the file doesn't exist.
    """
    if not _OPTIMAL_PARAMS_PATH.exists():
        return {}
    try:
        data = json.loads(_OPTIMAL_PARAMS_PATH.read_text())
        return {
            name: info.get("params", {})
            for name, info in data.get("setups", {}).items()
            if info.get("params")
        }
    except Exception as exc:
        log.warning(f"setup_loader: could not read optimal_params.json — {exc}")
        return {}


def load_setups(use_optimal_params: bool = True) -> list[BaseSetup]:
    """
    Discover and instantiate all BaseSetup subclasses in the Trading Setups folder.
    use_optimal_params=False forces default constructor args (used by tests so
    calibrated synthetic data is not broken by a previous hyperparameter run).
    Returns a list of setup instances.
    """
    if use_optimal_params in _CACHE:
        return list(_CACHE[use_optimal_params])

    instances: list[BaseSetup] = []
    seen_names: set[str] = set()
    optimal_params = _load_optimal_params() if use_optimal_params else {}

    if optimal_params:
        log.info(f"setup_loader: optimal params loaded for {len(optimal_params)} setup(s)")

    if not _SETUP_DIR.exists():
        log.warning(f"setup_loader: '{_SETUP_DIR}' not found — no setups loaded")
        return instances

    py_files = [
        f for f in _SETUP_DIR.glob("*.py")
        if not f.name.startswith("_") and f.name != "base_setup.py"
    ]

    for py_file in sorted(py_files):
        module_name = f"_trading_setup_{py_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                log.warning(f"setup_loader: cannot load spec for {py_file.name}")
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)  # type: ignore[attr-defined]
        except Exception as exc:
            log.error(f"setup_loader: error loading {py_file.name} — {exc}")
            continue

        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if (
                inspect.isclass(obj)
                and issubclass(obj, BaseSetup)
                and obj is not BaseSetup
                and getattr(obj, "name", "")  # non-empty name
            ):
                setup_name = obj.name
                if setup_name in seen_names:
                    log.warning(
                        f"setup_loader: duplicate setup name '{setup_name}' in {py_file.name} — skipping"
                    )
                    continue
                try:
                    raw_params = dict(optimal_params.get(setup_name, {}))
                    # sl_pct is a meta-param not accepted by any setup constructor
                    sl_pct      = raw_params.pop('sl_pct', None)
                    instance    = obj(**raw_params) if raw_params else obj()
                    if sl_pct is not None:
                        instance.sl_pct = sl_pct
                    instances.append(instance)
                    seen_names.add(setup_name)
                    display = {**raw_params, **({"sl_pct": sl_pct} if sl_pct is not None else {})}
                    suffix = f" (optimal params: {display})" if display else " (default params)"
                    log.info(f"setup_loader: registered '{setup_name}' from {py_file.name}{suffix}")
                except Exception as exc:
                    log.error(
                        f"setup_loader: cannot instantiate {attr_name} from {py_file.name} — {exc}"
                    )

    log.info(f"setup_loader: {len(instances)} setup(s) loaded")
    _CACHE[use_optimal_params] = instances
    return list(instances)
