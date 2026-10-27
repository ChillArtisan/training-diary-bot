"""Short-lived owner prompts kept in settings as pending:<key>, each with a timestamp."""
from __future__ import annotations

import json
import time

from diary.db import Db

MAX_AGE = 2 * 86400


def put(db: Db, key: str, value) -> None:
    db.set_json(f"pending:{key}", {"t": time.time(), "v": value})


def get(db: Db, key: str, max_age: float = MAX_AGE):
    """The stored value, or None when missing, stale or in the old unstamped format."""
    entry = _read(db.get(f"pending:{key}"))
    if entry is None or time.time() - entry["t"] > max_age:
        return None
    return entry["v"]


def drop(db: Db, key: str) -> None:
    db.unset(f"pending:{key}")


def _read(raw: str | None) -> dict | None:
    try:
        entry = json.loads(raw) if raw else None
    except ValueError:
        return None
    return entry if isinstance(entry, dict) and "t" in entry and "v" in entry else None


def prune(db: Db, max_age: float = MAX_AGE) -> int:
    """Drop expired entries; unstamped ones (written before stamping existed) get a stamp and live on."""
    gone = 0
    for row in db.q("SELECT key, value FROM settings WHERE key LIKE 'pending:%'"):
        entry = _read(row["value"])
        if entry is None:
            try:
                value = json.loads(row["value"])
            except ValueError:
                db.unset(row["key"])
                gone += 1
                continue
            db.set_json(row["key"], {"t": time.time(), "v": value})
        elif time.time() - entry["t"] > max_age:
            db.unset(row["key"])
            gone += 1
    return gone
