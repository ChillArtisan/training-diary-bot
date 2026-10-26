"""Where the owner is in the active program.

Position is sequence-based: the next day is the template day after the last
logged session, so a missed gym day does not silently shift the cycle.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .db import Db

MIN_OVERLAP = 0.3  # share of a day's exercises a log must contain to match it without a weekday


def assign_positions(db: Db) -> None:
    prog = db.active_program()
    with db.tx():
        db.clear_positions()
        if not prog:
            return
        days = db.program_days(prog["id"])
        if not days:
            return
        items = {d["id"]: {i["exercise_id"] for i in db.day_items(d["id"])} for d in days}
        per_week = max(1, sum(1 for d in days if d["week"] == days[0]["week"]))
        last_ord: int | None = None
        last_day: str | None = None
        for s in db.sessions_since(prog["date_utc"]):
            if s["post_id"] == prog["post_id"]:
                continue
            if s["week"] is None and s["weekday"] is None and s["day"] == last_day and last_ord is not None:
                db.set_position(s["id"], prog["id"], last_ord)  # the rest of a workout posted in parts
                continue
            exercises = db.session_exercise_ids(s["id"])
            if s["week"] is not None and any(d["week"] == s["week"] for d in days):
                pool = [d for d in days if d["week"] == s["week"]]
            elif last_ord is not None:
                pool = [days[(last_ord + 1 + k) % len(days)] for k in range(per_week + 1)]
            else:
                pool = list(days)
            day = _pick(pool, s, exercises, items)
            if day is not None:
                db.set_position(s["id"], prog["id"], day["ord"])
                last_ord, last_day = day["ord"], s["day"]


def _pick(pool, s, exercises: set[int], items: dict[int, set[int]]):
    if s["weekday"] is not None:
        same = [d for d in pool if d["weekday"] == s["weekday"]]
        if same:
            if len(same) > 1 and s["label"]:
                labeled = [d for d in same if d["label"].lower() == s["label"].lower()]
                same = labeled or same
            return max(same, key=lambda d: len(exercises & items[d["id"]]))  # max keeps the first on ties
    if not exercises:
        return None
    best = max(pool, key=lambda d: len(exercises & items[d["id"]]))
    overlap = len(exercises & items[best["id"]])
    if overlap and overlap >= MIN_OVERLAP * len(items[best["id"]]):
        return best
    return None


def _override(db: Db, program_id: int) -> dict | None:
    raw = db.get("cycle_next")
    if not raw:
        return None
    data = json.loads(raw)
    return data if data.get("program_id") == program_id else None


def next_day(db: Db):
    """The template day to train next, or None without an active program."""
    prog = db.active_program()
    if not prog:
        return None
    days = db.program_days(prog["id"])
    if not days:
        return None
    last = db.last_matched_session(prog["id"])
    ord_ = (last["ord"] + 1) % len(days) if last else 0
    ov = _override(db, prog["id"])
    if ov and (not last or ov["set_at"] > last["date_utc"]):
        ord_ = ov["ord"] % len(days)
    return days[ord_]


def set_next(db: Db, ord_: int) -> None:
    prog = db.active_program()
    if prog:
        db.set("cycle_next", json.dumps({
            "program_id": prog["id"], "ord": ord_, "set_at": datetime.now(timezone.utc).isoformat(),
        }))


def skip(db: Db):
    day = next_day(db)
    if day is None:
        return None
    n = len(db.program_days(day["program_id"]))
    set_next(db, (day["ord"] + 1) % n)
    return next_day(db)


def restart(db: Db):
    """Start the cycle over from the first day (after a long break)."""
    set_next(db, 0)
    return next_day(db)


def activate(db: Db, program_id: int) -> None:
    db.activate_program(program_id)
    assign_positions(db)
