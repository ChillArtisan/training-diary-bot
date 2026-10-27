"""Restoring the database from a *.db file the owner sends: validate, back up the current one, copy in."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from diary import backup, clock, program
from diary.db import MIGRATIONS, Db
from diary.ingest import reparse_if_needed

from .. import pending

REQUIRED = {"post", "session", "set_entry"}
PENDING_NAME = "restore-pending.db"


class RestoreError(Exception):
    """The message is meant for the owner."""


@dataclass
class Candidate:
    posts: int
    sessions: int


def pending_path(db: Db) -> Path:
    return db.path.parent / PENDING_NAME


def validate(path: Path, owner_id: int | None) -> Candidate:
    """Check that path is a diary database of this owner; raises RestoreError otherwise."""
    try:
        con = sqlite3.connect(str(path))
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RestoreError("Файл повреждён (integrity_check не прошёл).")
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not REQUIRED <= tables:
                raise RestoreError("Это не база дневника: нет таблиц с постами и тренировками.")
            if con.execute("PRAGMA user_version").fetchone()[0] > len(MIGRATIONS):
                raise RestoreError("База от более новой версии бота — её не открыть.")
            if "settings" in tables:
                row = con.execute("SELECT value FROM settings WHERE key = 'owner_id'").fetchone()
                if row and owner_id is not None and str(row[0]) != str(owner_id):
                    raise RestoreError("Эта база принадлежит другому владельцу.")
            return Candidate(con.execute("SELECT COUNT(*) FROM post").fetchone()[0],
                             con.execute("SELECT COUNT(*) FROM session").fetchone()[0])
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        raise RestoreError("Не открывается как база SQLite.") from e


def prepare(path: Path) -> None:
    """Bring the file's schema up to date (opening it as a Db runs the migrations), then leave one clean file."""
    Db(path).conn.close()
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def apply(db: Db, path: Path) -> Candidate:
    """Back up the live database, then copy the file into the open connection (no restart, no stale -wal)."""
    owner, channel = db.get("owner_id"), db.get("channel_id")
    folder = backup.backup_dir(db)
    folder.mkdir(parents=True, exist_ok=True)
    backup.snapshot(db, folder / f"pre-restore-{clock.now():%Y%m%d-%H%M%S}.db")
    src = sqlite3.connect(str(path))
    try:
        src.backup(db.conn)
    finally:
        src.close()
    db.conn.execute("PRAGMA foreign_keys = ON")
    for key, old in (("owner_id", owner), ("channel_id", channel)):
        if old and db.get(key) is None:
            db.set(key, old)  # a file from an unpaired copy must not unpair this bot
    pending.prune(db)
    reparse_if_needed(db)
    program.assign_positions(db)
    return Candidate(sum(db.post_counts().values()), db.counts()["sessions"])
