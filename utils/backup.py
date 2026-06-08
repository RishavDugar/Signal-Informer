"""
Database backup helpers.

Strategy:
  1. Checkpoint the WAL so the main .db file is fully up-to-date.
  2. Copy the .db file to db/backups/market_data_YYYYMMDD_HHMMSS.db.
  3. Verify the backup with PRAGMA integrity_check.
  4. Prune oldest backups, keeping MAX_BACKUPS.
"""

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from config import BACKUP_DIR, DB_PATH, MAX_BACKUPS
from utils.logger import get_logger

log = get_logger(__name__)


def create_backup() -> Path | None:
    """
    Create a timestamped backup of the live database.
    Returns the backup path on success, None on failure.
    Does NOT raise — backup failures are logged but never block ingestion.
    """
    if not DB_PATH.exists():
        log.warning("backup: database does not exist yet, skipping")
        return None

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"market_data_{stamp}.db"

    try:
        # Checkpoint WAL to ensure main file is current
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        conn.close()

        shutil.copy2(DB_PATH, dest)
        log.info(f"backup: created {dest.name}")

        # Verify backup integrity
        if not _integrity_ok(dest):
            log.error(f"backup: integrity check FAILED for {dest.name} — deleting corrupt backup")
            dest.unlink(missing_ok=True)
            return None

        _prune_old_backups()
        return dest

    except Exception as exc:
        log.error(f"backup: failed — {exc}")
        return None


def restore_latest_backup() -> bool:
    """
    Restore the most recent backup over the live database.
    Returns True on success. Use only for manual disaster recovery.
    """
    backups = sorted(BACKUP_DIR.glob("market_data_*.db"))
    if not backups:
        log.error("restore: no backups found")
        return False

    latest = backups[-1]
    try:
        if not _integrity_ok(latest):
            log.error(f"restore: backup {latest.name} failed integrity check")
            return False
        shutil.copy2(latest, DB_PATH)
        log.info(f"restore: restored from {latest.name}")
        return True
    except Exception as exc:
        log.error(f"restore: failed — {exc}")
        return False


def _integrity_ok(db_path: Path) -> bool:
    try:
        conn = sqlite3.connect(str(db_path))
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return result and result[0] == "ok"
    except Exception as exc:
        log.error(f"integrity check error on {db_path}: {exc}")
        return False


def _prune_old_backups() -> None:
    backups = sorted(BACKUP_DIR.glob("market_data_*.db"))
    excess = len(backups) - MAX_BACKUPS
    for old in backups[:excess]:
        try:
            old.unlink()
            log.debug(f"backup: pruned {old.name}")
        except Exception as exc:
            log.warning(f"backup: could not prune {old.name} — {exc}")
