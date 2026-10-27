"""Goals with a forecast: «жим 100 к лету» — when it comes at the current pace, and whether the deadline holds.

The pace is a robust trend (Theil–Sen: the median of pairwise slopes) over the sessions since the last
long break, so one great or one awful day does not move the forecast much.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from . import aliases, stats
from .db import Db
from .recommend import RESTART_AFTER_DAYS, fmt_num, is_deload

TREND_DAYS = 120          # the pace is measured over this window ...
MIN_POINTS = 4            # ... from at least this many sessions ...
MIN_SPAN_DAYS = 21        # ... spread over at least three weeks
MIN_PAIR_DAYS = 7         # pairs closer than a week are noise
FLAT_KG_WEEK = 0.1        # slower than this is «not growing»
FAR_DAYS = 183            # forecasts beyond half a year get a caution
MAX_DAYS = 730

GOAL_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<w>\d+(?:[.,]\d+)?)\s*(?:кг)?\s*(?:(?:x|х|×|\*|на)\s*(?P<r>\d{1,2}))?"
    r"(?:\s+(?P<when>(?:к|до|через)\s+.+))?$",
    re.I,
)
DATE_RE = re.compile(r"(?P<d>\d{1,2})[./](?P<m>\d{1,2})(?:[./](?P<y>\d{2,4}))?")
SEASONS = {"лету": (6, 1), "осени": (9, 1), "зиме": (12, 1), "весне": (3, 1), "новому году": (12, 31)}
IN_RE = re.compile(r"через\s+(?P<n>\d+)\s*(?P<u>нед|мес|дн)", re.I)
BODY_NAMES = {"вес", "вес тела", "масса"}


@dataclass
class Goal:
    id: int
    kind: str                 # lift | body
    exercise_id: int | None
    name: str
    target: float             # lift: one-rep max estimate; body: kg
    weight: float | None      # «80x5» is kept as written
    reps: int | None
    deadline: date | None
    created: date
    achieved: date | None

    @property
    def title(self) -> str:
        if self.kind == "body":
            return f"Вес тела: {fmt_num(self.target)} кг"
        if self.weight and self.reps and self.reps > 1:
            return f"{self.name}: {fmt_num(self.weight)}×{self.reps}"
        return f"{self.name}: {fmt_num(self.target)} кг на раз"


@dataclass
class Forecast:
    goal: Goal
    level: float | None = None        # where the trend is today
    best: float | None = None         # the best result lately
    pace: float | None = None         # kg per week
    eta: date | None = None
    eta_early: date | None = None
    eta_late: date | None = None
    at_deadline: float | None = None
    need_pace: float | None = None    # kg per week the deadline needs
    status: str = "no_data"           # done | on_track | late | eta | flat | wrong_way | no_data
    points: list[tuple[date, float]] | None = None


def _row(db: Db, r) -> Goal:
    name = "вес тела"
    if r["exercise_id"] is not None and (ex := db.exercise(r["exercise_id"])):
        name = ex["name"]
    return Goal(r["id"], r["kind"], r["exercise_id"], name, r["target"], r["weight"], r["reps"],
                date.fromisoformat(r["deadline"]) if r["deadline"] else None, date.fromisoformat(r["created"]),
                date.fromisoformat(r["achieved"]) if r["achieved"] else None)


def all_goals(db: Db, with_done: bool = False) -> list[Goal]:
    rows = db.q("SELECT * FROM goal" + ("" if with_done else " WHERE achieved IS NULL") + " ORDER BY id")
    return [_row(db, r) for r in rows]


def goal(db: Db, gid: int) -> Goal | None:
    r = db.one("SELECT * FROM goal WHERE id = ?", gid)
    return _row(db, r) if r else None


def set_goal(db: Db, today: date, kind: str, target: float, exercise_id: int | None = None,
             weight: float | None = None, reps: int | None = None, deadline: date | None = None) -> Goal:
    """One goal per exercise (and one for body weight): a new one replaces the old."""
    with db.tx():
        if kind == "body":
            db.conn.execute("DELETE FROM goal WHERE kind = 'body'")
        else:
            db.conn.execute("DELETE FROM goal WHERE kind = 'lift' AND exercise_id = ?", (exercise_id,))
        start = current_level(db, kind, exercise_id, today)
        gid = db.conn.execute(
            """INSERT INTO goal(kind, exercise_id, target, weight, reps, deadline, created, start_value)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (kind, exercise_id, target, weight, reps, deadline.isoformat() if deadline else None, today.isoformat(),
             start),
        ).lastrowid
    return goal(db, gid)


def drop_goal(db: Db, gid: int) -> bool:
    with db.tx():
        return db.conn.execute("DELETE FROM goal WHERE id = ?", (gid,)).rowcount > 0


# data --------------------------------------------------------------------------------------------

def lift_points(db: Db, eid: int) -> list[tuple[date, float]]:
    """Per session: the best one-rep max estimate (plain Epley, as in records); deloads left out."""
    bw = db.bodyweight()
    per: dict[int, list] = {}
    for r in db.exercise_sets(eid):
        if is_deload(r["session_rir"], r["session_phase"]):
            continue
        e = stats.set_e1rm(r["weight"], r["reps"], r["weight_kind"], bw)
        if e is None:
            continue
        cur = per.setdefault(r["session_id"], [date.fromisoformat(r["day"]), 0.0])
        cur[1] = max(cur[1], e)
    return sorted((d, e) for d, e in per.values())


def body_points(db: Db) -> list[tuple[date, float]]:
    return [(date.fromisoformat(r["day"]), r["weight"]) for r in db.body_series()]


def points(db: Db, kind: str, exercise_id: int | None) -> list[tuple[date, float]]:
    return body_points(db) if kind == "body" else lift_points(db, exercise_id)


def current_level(db: Db, kind: str, exercise_id: int | None, today: date) -> float | None:
    pts = points(db, kind, exercise_id)
    return max(v for _, v in pts[-3:]) if kind == "lift" and pts else (pts[-1][1] if pts else None)


def since_break(pts: list[tuple[date, float]], today: date) -> list[tuple[date, float]]:
    """The points after the last long break, inside the trend window."""
    start = 0
    for i in range(1, len(pts)):
        if (pts[i][0] - pts[i - 1][0]).days > RESTART_AFTER_DAYS:
            start = i
    return [p for p in pts[start:] if (today - p[0]).days <= TREND_DAYS]


def trend(pts: list[tuple[date, float]], today: date) -> tuple[float, float, float, float] | None:
    """Theil–Sen over the points -> (level today, kg/day, slow kg/day, fast kg/day), or None if too thin."""
    if len(pts) < MIN_POINTS or (pts[-1][0] - pts[0][0]).days < MIN_SPAN_DAYS:
        return None
    slopes = sorted((b[1] - a[1]) / (b[0] - a[0]).days
                    for i, a in enumerate(pts) for b in pts[i + 1:] if (b[0] - a[0]).days >= MIN_PAIR_DAYS)
    if len(slopes) < 3:
        return None
    slope = statistics.median(slopes)
    level = statistics.median(v + slope * (today - d).days for d, v in pts)
    q = statistics.quantiles(slopes, n=4)
    return level, slope, q[0], q[2]


# forecast ----------------------------------------------------------------------------------------

def _eta(today: date, left: float, per_day: float) -> date | None:
    if per_day <= 0:
        return None
    days = left / per_day
    return today + timedelta(days=round(days)) if days <= MAX_DAYS else None


def forecast(db: Db, g: Goal, today: date) -> Forecast:
    f = Forecast(g)
    pts = points(db, g.kind, g.exercise_id)
    f.points = pts
    if not pts:
        return f
    recent = since_break(pts, today)
    f.best = max(v for _, v in recent[-3:]) if recent and g.kind == "lift" else pts[-1][1]
    up = g.kind == "lift" or g.target >= (f.best or 0)
    reached = (f.best >= g.target) if up else (f.best <= g.target)
    if g.achieved or reached:
        f.status = "done"
        return f
    t = trend(recent, today)
    if t is None:
        return f
    f.level, slope, slow, fast = t
    if not up:
        slope, slow, fast = -slope, -fast, -slow
    f.pace = slope * 7 * (1 if up else -1)
    left = abs(g.target - f.level)
    if slope * 7 < FLAT_KG_WEEK:
        f.status = "flat" if slope * 7 > -FLAT_KG_WEEK else "wrong_way"
    else:
        f.eta = _eta(today, left, slope)
        f.eta_early = _eta(today, left, fast)
        f.eta_late = _eta(today, left, slow)
        f.status = "eta"
    if g.deadline:
        days = (g.deadline - today).days
        f.at_deadline = f.level + f.pace / 7 * days if days > 0 else f.level
        f.need_pace = left / days * 7 if days > 0 else None
        if f.status == "eta" and f.eta and f.eta <= g.deadline:
            f.status = "on_track"
        elif days > 0:
            f.status = "late"
    return f


def _d(d: date, today: date) -> str:
    return f"{d:%d.%m}" if d.year == today.year else f"{d:%d.%m.%y}"


def describe(f: Forecast, today: date) -> str:
    """Two short lines about a goal."""
    g = f.goal
    head = f"🏁 {g.title}" + (f" к {_d(g.deadline, today)}" if g.deadline else "")
    unit = "кг"
    if f.status == "done":
        return f"{head}\n✅ взята" + (f": сейчас ≈{fmt_num(round(f.best, 1))} {unit}" if f.best else "")
    if f.status == "no_data" or f.level is None:
        have = f"сейчас ≈{fmt_num(round(f.best, 1))} {unit}. " if f.best else ""
        what = "записи веса" if g.kind == "body" else "тренировки"
        return f"{head}\n{have}Для прогноза нужно хотя бы {MIN_POINTS} {what} за {MIN_SPAN_DAYS // 7} недели."
    now = f"сейчас ≈{fmt_num(round(f.level, 1))} {unit}"
    pace = f"{'+' if f.pace >= 0 else '−'}{fmt_num(round(abs(f.pace), 1))} кг/нед"
    if f.status in ("flat", "wrong_way"):
        line = f"{now}, темп {pace} — " + ("стоит на месте" if f.status == "flat" else "идёт в другую сторону")
        if g.deadline and f.need_pace:
            line += f". К сроку нужно {fmt_num(round(f.need_pace, 1))} кг/нед"
        return f"{head}\n{line}."
    when = f"примерно {_d(f.eta, today)}" if f.eta else "дальше двух лет"
    if f.eta and f.eta_early and f.eta_late and f.eta_late != f.eta_early:
        when += f" (между {_d(f.eta_early, today)} и {_d(f.eta_late, today)})"
    elif f.eta and f.eta_early and not f.eta_late:
        when += " (если темп удержится)"
    line = f"{now}, темп {pace} → {when}"
    if f.status == "on_track":
        line += ". К сроку успеваешь"
    elif f.status == "late":
        line += (f". К сроку по тренду будет ≈{fmt_num(round(f.at_deadline, 1))} {unit}: "
                 f"нужно {fmt_num(round(f.need_pace, 1))} кг/нед")
    if f.eta and (f.eta - today).days > FAR_DAYS:
        line += ". Прогноз дальше полугода — рост обычно замедляется"
    return f"{head}\n{line}."


def check_achieved(db: Db, today: date) -> list[Goal]:
    """Mark goals reached by the latest results; returns them (to congratulate once)."""
    done = []
    for g in all_goals(db):
        pts = points(db, g.kind, g.exercise_id)
        if not pts:
            continue
        last = pts[-1][1] if g.kind == "body" else max(v for _, v in pts[-3:])
        row = db.one("SELECT start_value FROM goal WHERE id = ?", g.id)
        start = row["start_value"] if row else None
        up = g.kind == "lift" or start is None or g.target >= start
        if (last >= g.target) if up else (last <= g.target):
            with db.tx():
                db.conn.execute("UPDATE goal SET achieved = ? WHERE id = ?", (today.isoformat(), g.id))
            g.achieved = today
            done.append(g)
    return done


def summary(db: Db, today: date) -> str:
    gs = all_goals(db)
    return "\n\n".join(describe(forecast(db, g, today), today) for g in gs)


# parsing «/goal жим лёжа 100 к 01.06» ------------------------------------------------------------------

def _deadline(text: str, today: date) -> date | None:
    text = text.lower().strip()
    if m := IN_RE.search(text):
        n, u = int(m["n"]), m["u"]
        return today + timedelta(days=n * (7 if u.startswith("нед") else 30 if u.startswith("мес") else 1))
    if m := DATE_RE.search(text):
        y = int(m["y"]) if m["y"] else today.year
        y = y + 2000 if y < 100 else y
        try:
            d = date(y, int(m["m"]), int(m["d"]))
        except ValueError:
            return None
        return d if m["y"] or d > today else d.replace(year=d.year + 1)
    for word, (mo, dd) in SEASONS.items():
        if word in text:
            d = date(today.year, mo, dd)
            return d if d > today else d.replace(year=d.year + 1)
    return None


@dataclass
class GoalRequest:
    kind: str
    name: str
    target: float
    weight: float | None
    reps: int | None
    deadline: date | None


def parse_goal(text: str, today: date) -> GoalRequest | None:
    """«жим лёжа 100», «присед 100x5 к 01.06», «вес 75 к лету», «тяга 140 через 3 месяца»."""
    m = GOAL_RE.match(text.strip())
    if not m:
        return None
    name = m["name"].strip(" :—-")
    w = float(m["w"].replace(",", "."))
    reps = int(m["r"]) if m["r"] else None
    deadline = _deadline(m["when"], today) if m["when"] else None
    if m["when"] and deadline is None:
        return None
    if aliases.norm(name) in BODY_NAMES:
        return GoalRequest("body", "вес тела", w, None, None, deadline)
    target = w * (1 + reps / 30) if reps and reps > 1 else w  # «80x5» means its one-rep max estimate
    return GoalRequest("lift", name, round(target, 1), w, reps, deadline)


def projection(f: Forecast, today: date, max_weeks: int = 26) -> list[tuple[date, float]] | None:
    """The trend line from today to the goal (or half a year ahead), for the chart."""
    if f.level is None or f.pace is None or f.pace <= 0 or f.status == "done":
        return None
    end = f.eta if f.eta and (f.eta - today).days <= max_weeks * 7 else today + timedelta(weeks=max_weeks)
    return [(today, f.level), (end, f.level + f.pace / 7 * (end - today).days)]


def lift_goal(db: Db, exercise_id: int) -> Goal | None:
    r = db.one("SELECT * FROM goal WHERE kind = 'lift' AND exercise_id = ? AND achieved IS NULL", exercise_id)
    return _row(db, r) if r else None
