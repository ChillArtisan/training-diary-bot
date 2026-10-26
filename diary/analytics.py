"""Training analytics: session efficiency (KPD), consistency, volume balance, insights.

Everything here returns plain data; diary/charts.py draws it and the bot formats it.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import movement, stats
from .db import Db
from .recommend import fmt_num

# KPD = weighted mean of the parts that can be measured for a session
W_COMPLETION, W_HIT, W_PROGRESS = 0.4, 0.4, 0.2
SAME_BAND = 0.005          # ±0.5% of the previous result counts as "held"
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAYS_FULL = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
PUSH = {"жим лёжа", "жим на наклонной", "жим вверх", "сведения", "трицепс", "средняя дельта"}
PULL = {"вертикальная тяга", "горизонтальная тяга", "задняя дельта", "бицепс", "шраги"}
LEGS = {"присед", "жим ногами", "выпады", "разгибание ног", "сгибание ног", "тяга с пола", "ягодичный мост", "икры"}
VOLUME_ZONE = (10, 20)     # working sets per muscle group per week, a common hypertrophy guideline
MUSCLES = {                # movement group -> muscle group the volume guideline is about
    "жим лёжа": "грудь", "жим на наклонной": "грудь", "сведения": "грудь",
    "вертикальная тяга": "спина", "горизонтальная тяга": "спина", "шраги": "спина",
    "жим вверх": "плечи", "средняя дельта": "плечи", "задняя дельта": "плечи",
    "бицепс": "бицепс", "трицепс": "трицепс",
    "присед": "квадрицепс", "жим ногами": "квадрицепс", "выпады": "квадрицепс", "разгибание ног": "квадрицепс",
    "тяга с пола": "задняя поверхность бедра", "сгибание ног": "задняя поверхность бедра",
    "ягодичный мост": "ягодицы", "икры": "икры", "пресс": "пресс",
}
RETURN_GAP_DAYS = 21       # a longer gap is a break; consistency is also shown since the return


@dataclass
class Kpd:
    session_id: int
    day: date
    title: str
    planned: int | None = None   # exercises in the matched program day
    done: int | None = None
    hit: int = 0                 # sets that reached the bottom of the target range
    graded: int = 0
    up: int = 0                  # exercises that beat their previous comparable session
    same: int = 0
    down: int = 0
    deload: bool = False

    @property
    def completion(self) -> float | None:
        return self.done / self.planned if self.planned else None

    @property
    def hit_rate(self) -> float | None:
        return self.hit / self.graded if self.graded else None

    @property
    def progress(self) -> float | None:
        n = self.up + self.same + self.down
        if self.deload or not n:
            return None
        return (self.up + 0.5 * self.same) / n

    @property
    def score(self) -> float | None:
        parts = [(v, w) for v, w in ((self.completion, W_COMPLETION), (self.hit_rate, W_HIT),
                                     (self.progress, W_PROGRESS)) if v is not None]
        if not parts:
            return None
        return 100 * sum(v * w for v, w in parts) / sum(w for _, w in parts)

    def text(self) -> str:
        if self.score is None:
            return "КПД не посчитать: нет программы и прошлых результатов для сравнения"
        bits = []
        if self.planned:
            bits.append(f"{self.done}/{self.planned} упражнений")
        if self.graded:
            bits.append(f"{round(100 * self.hit_rate)}% подходов в цели")
        if self.progress is not None:
            bits.append(f"прибавил в {self.up} из {self.up + self.same + self.down}")
        elif self.deload:
            bits.append("разгрузка — прогресс не оцениваю")
        return f"КПД {round(self.score)}% · " + " · ".join(bits)


def _metric(rows, bw) -> tuple[str, float] | None:
    """Comparable result of one exercise in one session: best e1RM, else best reps or seconds."""
    best = max((e for r in rows if (e := stats.set_e1rm(r["weight"], r["reps"], r["weight_kind"], bw))), default=None)
    if best:
        return "e1rm", best
    secs = max((r["seconds"] or 0 for r in rows), default=0)
    if secs:
        return "sec", secs
    reps = max((r["reps"] or 0 for r in rows if r["weight"] is None), default=0)
    return ("reps", reps) if reps else None


def kpd_series(db: Db) -> list[Kpd]:
    """KPD of every session, oldest first. One pass: previous results are tracked as we go."""
    bw = db.bodyweight()
    items: dict[tuple[int, int], list] = defaultdict(list)
    for it in db.all_program_items():
        items[(it["program_id"], it["day_ord"])].append(it)
    sets_by_session: dict[int, list] = defaultdict(list)
    for r in db.all_sets():
        sets_by_session[r["session_id"]].append(r)

    last_any: dict[int, tuple[str, float]] = {}             # exercise -> last result
    last_same_day: dict[tuple[int, int, int], tuple[str, float]] = {}  # (program, ord, exercise) -> result
    out = []
    for s in db.sessions_chrono():
        rows = sets_by_session.get(s["id"], [])
        if not rows:
            continue
        k = Kpd(s["id"], date.fromisoformat(s["day"]), s["title"] or "")
        plan = items.get((s["program_id"], s["ord"])) if s["ord"] is not None else None
        k.deload = (s["rir"] or 0) >= 4 or "разгруз" in (s["phase"] or "").lower()
        by_ex: dict[int, list] = defaultdict(list)
        for r in rows:
            by_ex[r["exercise_id"]].append(r)
        if plan:
            planned = {it["exercise_id"]: it for it in plan}
            k.planned, k.done = len(planned), len(planned.keys() & by_ex.keys())
            k.deload = k.deload or (plan[0]["day_rir"] or 0) >= 4 or "разгруз" in (plan[0]["phase"] or "").lower()
            for eid, ex_rows in by_ex.items():
                it = planned.get(eid)
                if not it or it["reps_min"] is None:
                    continue
                for r in ex_rows:
                    value = r["seconds"] if it["seconds"] else r["reps"]
                    if value is not None:
                        k.graded += 1
                        k.hit += value >= it["reps_min"]
        for eid, ex_rows in by_ex.items():
            m = _metric(ex_rows, bw)
            if m is None:
                continue
            key = (s["program_id"], s["ord"], eid) if s["ord"] is not None else None
            prev = last_same_day.get(key) if key else None
            prev = prev or last_any.get(eid)
            if prev and prev[0] == m[0] and prev[1]:
                change = m[1] / prev[1] - 1
                if change > SAME_BAND:
                    k.up += 1
                elif change < -SAME_BAND:
                    k.down += 1
                else:
                    k.same += 1
            last_any[eid] = m
            if key:
                last_same_day[key] = m
        out.append(k)
    return out


def post_kpd(db: Db, post_id: int) -> Kpd | None:
    ids = {r["id"] for r in db.q("SELECT id FROM session WHERE post_id = ?", post_id)}
    found = [k for k in kpd_series(db) if k.session_id in ids]
    return found[-1] if found else None


# ---------- consistency ----------

def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


@dataclass
class Consistency:
    weeks: list[tuple[date, int]]            # completed weeks in the window: (monday, training days)
    planned: int
    adherence: float | None                  # done / planned over the window, capped per week
    avg_per_week: float
    current_streak: int
    best_streak: int
    days_since: int | None
    longest_break: int
    weekday_share: dict[int, tuple[int, int]] = field(default_factory=dict)  # program weekday -> (weeks hit, weeks)
    training_days: set[date] = field(default_factory=set)
    since_return: tuple[date, float, int] | None = None     # after a long break: (first day back, adherence, weeks)


def consistency(db: Db, today: date, weeks: int = 12) -> Consistency:
    days = {date.fromisoformat(s["day"]) for s in db.sessions_chrono()}
    planned = stats.planned_per_week(db)
    this_week = week_start(today)
    first = week_start(min(days)) if days else this_week
    window = [this_week - timedelta(weeks=i) for i in range(weeks, 0, -1)]
    window = [w for w in window if w >= first]
    per_week = [(w, sum(1 for d in days if w <= d < w + timedelta(days=7))) for w in window]
    adherence = (sum(min(n, planned) for _, n in per_week) / (planned * len(per_week))) if per_week else None
    avg = sum(n for _, n in per_week) / len(per_week) if per_week else 0.0

    # streaks over the whole history (a week counts with at least planned - 1 training days)
    need = max(1, planned - 1)
    best = cur = 0
    w = first
    while w < this_week:
        if sum(1 for d in days if w <= d < w + timedelta(days=7)) >= need:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
        w += timedelta(weeks=1)
    current = stats.streak(db, today)

    in_window = sorted(d for d in days if window and d >= window[0])
    gaps = [(b - a).days for a, b in zip(in_window, in_window[1:])]
    if in_window:
        gaps.append((today - in_window[-1]).days)
    since_return = None
    ordered = sorted(days)
    back = [b for a, b in zip(ordered, ordered[1:]) if (b - a).days > RETURN_GAP_DAYS]
    share_window = window
    if back and window and back[-1] > window[0]:
        weeks_back = [(w, n) for w, n in per_week if w >= week_start(back[-1])]
        if weeks_back:
            since_return = (back[-1], sum(min(n, planned) for _, n in weeks_back) / (planned * len(weeks_back)),
                            len(weeks_back))
            share_window = [w for w, _ in weeks_back]  # weekday habits are about the current run, not the break

    weekday_share: dict[int, tuple[int, int]] = {}
    if share_window:
        for wd in sorted(planned_weekdays(db)):
            hit = sum(1 for w0 in share_window if (w0 + timedelta(days=wd)) in days)
            weekday_share[wd] = (hit, len(share_window))
    return Consistency(per_week, planned, adherence, avg, current, max(best, current),
                       (today - max(days)).days if days else None, max(gaps, default=0), weekday_share, days,
                       since_return)


# ---------- volume and strength trends ----------

def weekly_volume(db: Db, today: date, days: int = 28, by: str = "movement") -> dict[str, float]:
    """Working sets per movement group (or per muscle group, by="muscle") per week over the last `days`."""
    first = (today - timedelta(days=days - 1)).isoformat()
    counts: dict[str, int] = defaultdict(int)
    for r in db.all_sets():
        if r["day"] >= first:
            group = movement.group_of(r["exercise"]) or "прочее"
            counts[MUSCLES.get(group, "прочее") if by == "muscle" else group] += 1
    weeks = days / 7
    return {g: n / weeks for g, n in sorted(counts.items(), key=lambda x: -x[1])}


@dataclass
class Trend:
    exercise_id: int
    name: str
    now: float
    before: float

    @property
    def change(self) -> float:
        return self.now / self.before - 1


def strength_trends(db: Db, today: date, recent_days: int = 28, back_days: tuple[int, int] = (56, 112)) -> list[Trend]:
    """Best e1RM of the last four weeks vs two to four months ago, for every weighted exercise."""
    out = []
    for x in db.exercises_used():
        series = [(date.fromisoformat(d), e) for d, e in stats.e1rm_series(db, x["id"])]
        now = [e for d, e in series if (today - d).days < recent_days]
        before = [e for d, e in series if back_days[0] <= (today - d).days <= back_days[1]]
        if now and before:
            out.append(Trend(x["id"], x["name"], max(now), max(before)))
    return sorted(out, key=lambda t: -t.change)


def main_lifts(db: Db, today: date, days: int = 120, limit: int = 8) -> list[tuple[int, str]]:
    """Weighted exercises done most often lately — the ones worth a strength chart."""
    first = (today - timedelta(days=days)).isoformat()
    counts: dict[int, set] = defaultdict(set)
    names: dict[int, str] = {}
    for r in db.all_sets():
        if r["day"] >= first and r["weight"] is not None and r["weight_kind"] in stats.WEIGHTED:
            counts[r["exercise_id"]].add(r["session_id"])
            names[r["exercise_id"]] = r["exercise"]
    barbell = {e for e in counts if (x := db.exercise(e)) and x["equipment"] == "barbell"}
    ranked = sorted(counts, key=lambda e: (e not in barbell, -len(counts[e])))  # big lifts first
    return [(e, names[e]) for e in ranked if len(counts[e]) >= 3][:limit]


# ---------- chart data (plain values for diary/charts.py) ----------

def strength_points(db: Db, eid: int) -> list[tuple[date, float, float | None]]:
    """Per session: best e1RM and the top working weight."""
    bw = db.bodyweight()
    per: dict[int, list] = {}
    for r in reversed(db.exercise_sets(eid)):
        e = stats.set_e1rm(r["weight"], r["reps"], r["weight_kind"], bw)
        if e is None:
            continue
        cur = per.setdefault(r["session_id"], [date.fromisoformat(r["day"]), 0.0, None])
        cur[1] = max(cur[1], e)
        if r["weight_kind"] in ("abs", "smith"):
            cur[2] = max(cur[2] or 0, r["weight"])
    return sorted(tuple(v) for v in per.values())


def weekly_series(db: Db, today: date, weeks: int = 16) -> list[tuple[date, int, float]]:
    this_week = week_start(today)
    days = {date.fromisoformat(s["day"]) for s in db.sessions_chrono()}
    out = []
    for i in range(weeks - 1, -1, -1):
        w = this_week - timedelta(weeks=i)
        rows = db.sets_between(w.isoformat(), (w + timedelta(days=6)).isoformat())
        out.append((w, sum(1 for d in days if w <= d < w + timedelta(days=7)), stats.tonnage(rows) / 1000))
    return out


def kpd_points(db: Db, last: int = 30) -> list[tuple[date, float]]:
    return [(k.day, k.score) for k in kpd_series(db) if k.score is not None][-last:]


def bodyweight_points(db: Db) -> list[tuple[date, float]]:
    return [(date.fromisoformat(r["day"]), r["weight"]) for r in db.body_series()]


def planned_weekdays(db: Db) -> set[int]:
    prog = db.active_program()
    return {d["weekday"] for d in db.program_days(prog["id"]) if d["weekday"] is not None} if prog else set()


# ---------- the report ----------

def _pct(v: float) -> str:
    return f"{round(v * 100)}%"


def insights(db: Db, today: date) -> list[str]:
    out = []
    c = consistency(db, today)
    if c.adherence is not None:
        out.append(f"Постоянство за {len(c.weeks)} нед.: {_pct(c.adherence)} — в среднем "
                   f"{fmt_num(round(c.avg_per_week, 1))} тренировки в неделю из {c.planned}.")
    if c.since_return:
        day, adh, n = c.since_return
        out.append(f"С возвращения после перерыва ({day:%d.%m}): {_pct(adh)} за {n} нед.")
    if c.current_streak >= 2 or c.best_streak >= 2:
        out.append(f"Серия: {c.current_streak} нед. подряд (рекорд — {c.best_streak}).")
    weak = [(wd, h, n) for wd, (h, n) in c.weekday_share.items() if n and h / n < 0.7]
    if weak:
        wd, h, n = min(weak, key=lambda x: x[1] / x[2])
        out.append(f"Чаще всего выпадает {WEEKDAYS_FULL[wd]}: был в зале {h} из {n} недель.")
    if c.days_since is not None and c.days_since >= 5:
        out.append(f"Последняя тренировка {c.days_since} дн. назад.")

    series = [k for k in kpd_series(db) if k.score is not None]
    recent = [k.score for k in series if (today - k.day).days < 28]
    before = [k.score for k in series if 28 <= (today - k.day).days < 56]
    if recent:
        line = f"КПД тренировок за 4 недели: {round(sum(recent) / len(recent))}%"
        if before:
            line += f" (до этого {round(sum(before) / len(before))}%)"
        out.append(line + ".")

    trends = strength_trends(db, today)
    ups = [t for t in trends if t.change > 0.01][:3]
    if ups:
        out.append("Растёт сила: " + "; ".join(f"{t.name} +{_pct(t.change)}" for t in ups) + ".")
    downs = [t for t in trends if t.change < -0.05]
    if downs:
        t = min(downs, key=lambda t: t.change)
        out.append(f"Просело: {t.name} {_pct(t.change)} к прошлым месяцам — проверь сон, питание и не пора ли разгрузка.")

    vol = weekly_volume(db, today)
    push = sum(v for g, v in vol.items() if g in PUSH)
    pull = sum(v for g, v in vol.items() if g in PULL)
    if push and pull and not 0.77 <= push / pull <= 1.3:
        more, less = ("жимов", "тяг") if push > pull else ("тяг", "жимов")
        out.append(f"Перекос: {more} {fmt_num(round(max(push, pull)))} подх./нед. против "
                   f"{fmt_num(round(min(push, pull)))} у {less}.")
    muscles = weekly_volume(db, today, by="muscle")
    low = [m for m, v in muscles.items() if v < VOLUME_ZONE[0] and m not in ("пресс", "прочее")]
    if low and len(low) <= 4:
        out.append(f"Меньше {VOLUME_ZONE[0]} подходов в неделю: {', '.join(low)} — для роста мышц маловато.")
    elif low:
        out.append(f"Объём ниже ориентира {VOLUME_ZONE[0]}–{VOLUME_ZONE[1]} подходов в неделю почти везде — "
                   "это нормально для силового блока, но для массы мало.")
    return out


def report_text(db: Db, today: date) -> str:
    lines = insights(db, today)
    return "\n".join(f"• {ln}" for ln in lines) if lines else "Данных пока мало: нужно хотя бы несколько тренировок."
