"""Owner's temporary states: pause (illness, trip) and exercises to avoid (pain)."""
from __future__ import annotations

from datetime import date

from .db import Db

FOREVER = "9999-12-31"


def paused_until(db: Db) -> date | None:
    raw = db.get("pause_until")
    return date.fromisoformat(raw) if raw else None


def is_paused(db: Db, today: date) -> bool:
    until = paused_until(db)
    since = db.get("pause_from") or "0000-01-01"
    return until is not None and since <= today.isoformat() and today <= until


def pause(db: Db, today: date, until: date | None) -> None:
    db.set("pause_from", today.isoformat())
    db.set("pause_until", until.isoformat() if until else FOREVER)


def resume(db: Db) -> None:
    db.unset("pause_until")
    db.unset("pause_from")


def avoided(db: Db) -> set[int]:
    return set(db.get_json("avoid", []))


def avoid(db: Db, eid: int) -> None:
    db.set_json("avoid", sorted(avoided(db) | {eid}))


def allow(db: Db, eid: int) -> None:
    db.set_json("avoid", sorted(avoided(db) - {eid}))
