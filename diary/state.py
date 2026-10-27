"""Owner's temporary states: pause (illness, trip) and exercises to avoid (pain)."""
from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from . import clock
from .db import Db

if TYPE_CHECKING:
    from .today import Plan

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
    end = until.isoformat() if until else FOREVER
    log = db.get_json("pauses", [])
    if log and log[-1][0] <= today.isoformat() <= log[-1][1]:
        log[-1][1] = end  # extending or shortening the pause that is already on
    else:
        log.append([today.isoformat(), end])
    db.set_json("pauses", log)


def resume(db: Db, today: date | None = None) -> None:
    today = today or clock.today()
    log = db.get_json("pauses", [])
    if log and log[-1][1] >= today.isoformat():
        log[-1][1] = max(log[-1][0], today.isoformat())  # the pause stays in the log, closed today
        db.set_json("pauses", log)
    db.unset("pause_until")
    db.unset("pause_from")


def paused_days(db: Db, today: date | None = None) -> list[tuple[date, date]]:
    """Pause intervals (first day, last day), the open one cut at `today`; future days are dropped."""
    today = today or clock.today()
    log = [list(x) for x in db.get_json("pauses", [])]
    since, until = db.get("pause_from"), db.get("pause_until")
    if since and until and not any(x[0] == since for x in log):
        log.append([since, until])  # a pause set before the log existed
    out = []
    for first, last in sorted(log):
        start, end = date.fromisoformat(first), min(date.fromisoformat(last), today)
        if start <= end:
            out.append((start, end))
    return out


def paused_weeks(db: Db, today: date | None = None, min_days: int = 4) -> set[date]:
    """Mondays of weeks with at least `min_days` paused days: they do not count as misses."""
    paused: set[date] = set()
    for first, last in paused_days(db, today):
        paused.update(first + timedelta(days=i) for i in range((last - first).days + 1))
    count: dict[date, int] = {}
    for d in paused:
        monday = d - timedelta(days=d.weekday())
        count[monday] = count.get(monday, 0) + 1
    return {w for w, n in count.items() if n >= min_days}


def avoided(db: Db) -> set[int]:
    return set(db.get_json("avoid", []))


def avoid(db: Db, eid: int) -> None:
    db.set_json("avoid", sorted(avoided(db) | {eid}))


def allow(db: Db, eid: int) -> None:
    db.set_json("avoid", sorted(avoided(db) - {eid}))


def note_plan(db: Db, day: date, plan: Plan) -> None:
    """Remember the plan shown for a day (mode, exercises, sets): KPD judges the session against it."""
    items = [[ln.eid, ln.sets] for ln in plan.lines if not ln.skip and ln.eid is not None]
    db.set_json(f"plan:{day.isoformat()}", {"day_id": plan.day_id, "mode": plan.mode, "items": items})


def plan_note(db: Db, day: date | str) -> dict | None:
    """{"day_id": int, "mode": "normal"|"light"|"short", "items": [[exercise_id, sets], ...]} or None."""
    key = day if isinstance(day, str) else day.isoformat()
    return db.get_json(f"plan:{key}", None)
