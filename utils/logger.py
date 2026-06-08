import io
import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

from config import BASE_DIR

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

_FMT = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _safe_stdout_stream():
    """
    Return a stdout stream that never crashes on Windows cp1252 consoles.
    If stdout has a binary buffer (normal case), wrap it with UTF-8 + errors='replace'
    so any non-ASCII log message is printed with '?' substitutions rather than raising.
    """
    if hasattr(sys.stdout, "buffer"):
        return io.TextIOWrapper(
            sys.stdout.buffer,
            encoding="utf-8",
            errors="replace",
            line_buffering=True,
        )
    return sys.stdout


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Rotating file handler — 5 MB per file, keep 5 files (always UTF-8)
    fh = RotatingFileHandler(
        LOG_DIR / "signal_infomer.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FMT, _DATE_FMT))

    # Console handler — INFO and above, UTF-8 safe on Windows
    ch = logging.StreamHandler(_safe_stdout_stream())
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(_FMT, _DATE_FMT))

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger
