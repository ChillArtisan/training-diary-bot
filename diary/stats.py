"""Records, progress and summaries. Read-only over the database."""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date, timedelta

from .db import Db
from .recommend import fmt_num, fmt_weight

WEIGHTED = ("abs", "smith", "added")
MAX_REPS_FOR_E1RM = 12  # Epley gets silly on long sets
SPARK = "▁▂▃▄▅▆▇█"


def set_e1rm(weight: float | None, reps: int | None, kind: str, bodyweight: float | None = None) -> float | None:
    """Plain Epley on the reps actually done; RIR guesses do not belong in records."""
    if weight is None or not reps or kind not in WEIGHTED or reps > MAX_REPS_FOR_E1RM:
        return None
    if kind == "added":
        if not bodyweight:
            return None
        weight += bodyweight
    return weight * (1 + reps / 30) if reps > 1 else weight


@dataclass
class Record:
    exercise: str
    weight: float
    reps: int
    kind: str
    e1rm: float
    previous: float

    def text(self) -> str:
        return (f"{self.exercise}: {fmt_weight(self.weight, self.kind)}×{self.reps} → "
                f"максимум на 1 повтор ≈{fmt_num(round(self.e1rm))} кг (было ≈{fmt_num(round(self.previous))})")


def _best(rows, bw) -> tuple[float, object] | None:
    best = None
    for r in rows:
        e = set_e1rm(r["weight"], r["reps"], r["weight_kind"], bw)
        if e is not None and (best is None or e > best[0]):
            best = (e, r)
    return best


def post_records(db: Db, post_id: int) -> list[Record]:
    """Exercises in this post whose best set beats everything logged before it."""
    post = db.post(post_id)
    if not post:
        return []
    bw = db.bodyweight()
    by_ex: dict[int, list] = {}
    for r in db.post_sets(post_id):
        by_ex.setdefault(r["exercise_id"], []).append(r)
    out = []
    for eid, rows in by_ex.items():
        now = _best(rows, bw)
        before = _best(db.sets_before(eid, post["date_utc"]), bw)
        if now and before and now[0] > before[0] + 0.25:
            r = now[1]
            out.append(Record(db.exercise(eid)["name"], r["weight"], r["reps"], r["weight_kind"], now[0], before[0]))
    return out


def sparkline(values: list[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return SPARK[3] * len(values)
    return "".join(SPARK[round((v - lo) / (hi - lo) * (len(SPARK) - 1))] for v in values)


def e1rm_series(db: Db, eid: int) -> list[tuple[str, float]]:
    """Best estimated max per session, oldest first."""
    bw = db.bodyweight()
    best: dict[int, tuple[str, float]] = {}
    for r in reversed(db.exercise_sets(eid)):
        e = set_e1rm(r["weight"], r["reps"], r["weight_kind"], bw)
        if e is None:
            continue
        sid = r["session_id"]
        if sid not in best or e > best[sid][1]:
            best[sid] = (r["day"], e)
    return sorted(best.values())


def progress_text(db: Db, eid: int) -> str:
    name = db.exercise(eid)["name"]
    series = e1rm_series(db, eid)
    if not series:
        return f"{name}: нет подходов с весом"
    monthly: dict[str, float] = {}
    for day, e in series:
        monthly[day[:7]] = max(monthly.get(day[:7], 0), e)
    months = list(monthly.items())[-12:]
    top_day, top = max(series, key=lambda x: x[1])
    first, last = series[0][1], series[-1][1]
    lines = [
        f"{name}",
        f"{sparkline([v for _, v in months])}  {months[0][0]} → {months[-1][0]}",
        f"Максимум на 1 повтор (оценка, кг): сейчас ≈{fmt_num(round(last))}, лучший ≈{fmt_num(round(top))} "
        f"({date.fromisoformat(top_day):%d.%m.%y}), с начала {'+' if last >= first else ''}{fmt_num(round(last - first, 1))} кг",
    ]
    return "\n".join(lines)


def week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def tonnage(rows) -> float:
    return sum((r["weight"] or 0) * (r["reps"] or 0) for r in rows if r["weight_kind"] in ("abs", "smith"))


def planned_per_week(db: Db) -> int:
    prog = db.active_program()
    if not prog:
        return 3
    days = db.program_days(prog["id"])
    first_week = days[0]["week"] if days else 1
    return max(1, sum(1 for d in days if d["week"] == first_week))


def training_days(db: Db, first: date, last: date) -> int:
    return len({s["day"] for s in db.sessions_between(first.isoformat(), last.isoformat())})


def streak(db: Db, today: date) -> int:
    """Weeks in a row (ending with the current or last week) with enough training days."""
    need = max(1, planned_per_week(db) - 1)
    start, _ = week_bounds(today)
    if training_days(db, start, start + timedelta(days=6)) < need:
        start -= timedelta(days=7)  # the current week may still be in progress
    n = 0
    while training_days(db, start, start + timedelta(days=6)) >= need:
        n += 1
        start -= timedelta(days=7)
    return n


def records_between(db: Db, first: date, last: date) -> list[str]:
    """Running-max records for sessions inside [first, last]."""
    bw = db.bodyweight()
    best: dict[int, float] = {}
    out: dict[str, float] = {}
    for r in db.all_sets():
        e = set_e1rm(r["weight"], r["reps"], r["weight_kind"], bw)
        if e is None:
            continue
        prev = best.get(r["exercise_id"])
        if prev is not None and e > prev + 0.25 and first.isoformat() <= r["day"] <= last.isoformat():
            out[r["exercise"]] = max(out.get(r["exercise"], 0), e)
        if prev is None or e > prev:
            best[r["exercise_id"]] = e
    return [f"{name} — ≈{fmt_num(round(e))} кг на 1 повтор" for name, e in out.items()]


def body_change(db: Db, first: date, last: date) -> str | None:
    series = [(date.fromisoformat(r["day"]), r["weight"]) for r in db.body_series()]
    inside = [w for d, w in series if first <= d <= last]
    before = [w for d, w in series if d < first]
    if not inside:
        return None
    now = inside[-1]
    if before:
        diff = now - before[-1]
        return f"{fmt_num(now)} кг ({'+' if diff >= 0 else ''}{fmt_num(round(diff, 1))})"
    return f"{fmt_num(now)} кг"


def weekly_summary(db: Db, today: date) -> str:
    first, last = week_bounds(today)
    days = training_days(db, first, last)
    plan = planned_per_week(db)
    rows = db.sets_between(first.isoformat(), last.isoformat())
    prev_rows = db.sets_between((first - timedelta(days=7)).isoformat(), (last - timedelta(days=7)).isoformat())
    t, t_prev = tonnage(rows), tonnage(prev_rows)
    lines = [f"Неделя {first:%d.%m}–{last:%d.%m}", f"Тренировок: {days} из {plan}"]
    if t:
        delta = f" ({'+' if t >= t_prev else ''}{round((t - t_prev) / t_prev * 100)}% к прошлой)" if t_prev else ""
        lines.append(f"Тоннаж: {fmt_num(round(t / 1000, 1))} т{delta}")
    if recs := records_between(db, first, last):
        lines.append("Рекорды: " + "; ".join(recs))
    if bw := body_change(db, first, last):
        lines.append(f"Вес тела: {bw}")
    s = streak(db, today)
    if s >= 2:
        lines.append(f"Серия: {s} недель подряд")
    if days == 0:
        lines.append("Неделя пустая. Если болеешь или уехал — /pause, напоминания замолчат.")
    if n := len(db.open_suggestions()):
        lines.append(f"Вопросов по названиям упражнений: {n} — /merge")
    return "\n".join(lines)


MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь"]
EQUIPMENT_TAG = {"machine": "тренажёр", "cable": "блок", "dumbbell": "одна гантель"}
STALE_BODYWEIGHT_DAYS = 30


def _month_line(db: Db, first: date, last: date) -> str:
    t = tonnage(db.sets_between(first.isoformat(), last.isoformat()))
    n = training_days(db, first, last)
    per = f" (≈{fmt_num(round(t / n / 1000, 1))} т за тренировку)" if n else ""
    return f"• {MONTHS[first.month - 1]}: {fmt_num(round(t / 1000, 1))} т за {n} трен.{per}"


def best_sets(db: Db, min_sessions: int = 3) -> list[tuple[float, str, object]]:
    """Per exercise: the set with the highest estimated one-rep max -> (estimate, name, set row)."""
    bw = db.bodyweight()
    out = []
    for x in db.exercises_used():
        if x["sessions"] < min_sessions:
            continue
        best = _best(db.exercise_sets(x["id"]), bw)
        if best:
            out.append((best[0], x["name"], best[1]))
    return sorted(out, key=lambda b: -b[0])


def overall(db: Db, today: date) -> str:
    c = db.counts()
    first = db.one("SELECT MIN(day) d FROM session")["d"]
    if not first:
        return "Тренировок пока нет."
    planned = planned_per_week(db)
    recent = training_days(db, today - timedelta(days=27), today)
    month_start = today.replace(day=1)
    prev_end = month_start - timedelta(days=1)
    lines = [
        "<b>Общая картина</b>",
        f"Тренировок: {c['sessions']} с {date.fromisoformat(first):%d.%m.%y}, за последние 4 недели — {recent}",
        f"Серия: {streak(db, today)} нед. подряд (неделя засчитывается от {max(1, planned - 1)} трен. из {planned})",
        "",
        "<b>Тоннаж</b> — сумма «вес × повторы» по всем подходам со штангой, гантелями и в тренажёрах:",
        _month_line(db, month_start, today),
        _month_line(db, prev_end.replace(day=1), prev_end),
    ]
    tops = best_sets(db)
    free = [b for b in tops if b[2]["weight_kind"] != "added"][:6]
    loaded = [b for b in tops if b[2]["weight_kind"] == "added"][:3]
    if free:
        lines += ["", "<b>Максимум на 1 повтор</b> — оценка в кг по лучшему подходу (формула Эпли):"]
        for e, name, r in free:
            tag = EQUIPMENT_TAG.get(db.exercise(r["exercise_id"])["equipment"])
            lines.append(f"• {html.escape(name)}{f' ({tag})' if tag else ''} — ≈{fmt_num(round(e))} кг: "
                         f"{fmt_weight(r['weight'], r['weight_kind'])}×{r['reps']}, {date.fromisoformat(r['day']):%d.%m.%y}")
        if any(EQUIPMENT_TAG.get(db.exercise(b[2]["exercise_id"])["equipment"]) for b in free):
            lines.append("<i>Тренажёры и блоки со штангой не сравнивай: у них своя механика и свои цифры.</i>")
    bw = db.bodyweight()
    if loaded and bw:
        lines += ["", f"<b>Со своим весом</b> — свой вес {fmt_num(bw)} кг плюс отягощение, оценка на 1 повтор:"]
        for e, name, r in loaded:
            lines.append(f"• {html.escape(name)} — ≈{fmt_num(round(e))} кг: {fmt_weight(r['weight'], 'added')}×{r['reps']}, "
                         f"{date.fromisoformat(r['day']):%d.%m.%y}")
    body = db.one("SELECT day, weight FROM body ORDER BY day DESC LIMIT 1")
    if body:
        day = date.fromisoformat(body["day"])
        line = f"Вес тела: {fmt_num(body['weight'])} кг на {day:%d.%m.%y}"
        if (today - day).days > STALE_BODYWEIGHT_DAYS:
            line += f" — это {(today - day).days} дн. назад; напиши в канал свежий, например «70,6кг»"
        lines += ["", line]
    return "\n".join(lines)


def measurements_text(db: Db) -> str:
    days = db.measurement_days(2)
    if not days:
        return "Замеров нет. Пост вида «Замеры … Обхват груди: 95» попадёт сюда."
    (d1, now), *rest = days
    prev = rest[0][1] if rest else {}
    lines = [f"Замеры на {date.fromisoformat(d1):%d.%m.%y}" + (f" (сравнение с {date.fromisoformat(rest[0][0]):%d.%m.%y})" if rest else "")]
    for k, v in sorted(now.items()):
        diff = f" ({'+' if v - prev[k] >= 0 else ''}{fmt_num(round(v - prev[k], 1))})" if k in prev else ""
        lines.append(f"• {k}: {fmt_num(v)}{diff}")
    return "\n".join(lines)
