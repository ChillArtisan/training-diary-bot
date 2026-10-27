"""Database backups and a CSV export of every set."""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date
from pathlib import Path

from .db import Db

KEEP = 14


def backup_dir(db: Db) -> Path:
    return db.path.parent / "backups"


def snapshot(db: Db, path: Path) -> Path:
    """Consistent single-file copy via the SQLite backup API (safe while the bot writes).
    The copy is switched out of WAL, so a leftover -wal file can never undo a restore."""
    dst = sqlite3.connect(str(path))
    try:
        db.conn.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
    return path


def make_backup(db: Db, day: date, keep: int = KEEP) -> Path:
    """Dated snapshot in backups/, oldest copies rotated out."""
    folder = backup_dir(db)
    folder.mkdir(parents=True, exist_ok=True)
    path = snapshot(db, folder / f"diary-{day.isoformat()}.db")
    for old in sorted(folder.glob("diary-*.db"))[:-keep]:
        old.unlink()
    db.set("last_backup", day.isoformat())
    return path


def latest_backup(db: Db) -> Path | None:
    files = sorted(backup_dir(db).glob("diary-*.db"))
    return files[-1] if files else None


def export_csv(db: Db) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "exercise", "set", "reps", "seconds", "weight", "weight_kind", "rir", "line"])
    for r in db.all_sets():
        w.writerow([r["day"], r["exercise"], r["set_idx"] + 1, r["reps"], r["seconds"], r["weight"],
                    r["weight_kind"], r["rir"], r["raw"]])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens Cyrillic correctly
