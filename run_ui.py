"""
Launcher for the Signal Infomer web dashboard.

    python run_ui.py                 # http://127.0.0.1:5000
    python run_ui.py --port 8080
    python run_ui.py --no-browser
    python run_ui.py --host 0.0.0.0  # expose on LAN (use with care)

A dark-themed control panel for every backend feature: run the technical /
news pipelines, the backtester and hyperparameter search, browse signals,
news & scout picks, setups, stored OHLCV, edit .env config, tail logs, and
manage the Windows scheduler task.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

# Ensure repo root is importable (config, data.db, notifications, ...)
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal Infomer web dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    from data.db import init_db
    init_db()

    from webui.server import app

    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}"
    print("=" * 60)
    print(f"  Signal Infomer dashboard -> {url}")
    print("=" * 60)

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # use_reloader=False so the subprocess job manager / background threads
    # aren't duplicated by Flask's reloader.
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
