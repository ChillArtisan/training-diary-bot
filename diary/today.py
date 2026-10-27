"""Build and render the next training day with recommended weights."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date

from . import movement, plates, program, state
from .db import Db
from .recommend import (
    DEFAULT_RIR, LIGHT_FACTOR, RESTART_AFTER_DAYS, Perf, Rec, Target, fmt_weight, gap_factor, is_deload, recommend,
    round_weight,
)

PCT_RE = re.compile(r"(?P<a>\d{2})(?:\s*[-–]\s*(?P<b>\d{2}))?\s*%\s*(?:от\s*)?(?:1?\s*пм|1?\s*rm)", re.I)
MODES = {"normal": "", "light": "лёгкий день", "short": "коротко"}


@dataclass
class PlanLine:
    name: str
    scheme: str
    rec: Rec
    skip: bool = False
    eid: int | None = None   # the exercise actually planned (the replacement for an avoided one)
    sets: int | None = None

    @property
    def text(self) -> str:
        w = fmt_weight(self.rec.weight, self.rec.kind)
        return f"{self.name} – {self.scheme} {w}".rstrip()


@dataclass
class Plan:
    week_title: str | None
    title: str
    lines: list[PlanLine]
    mode: str = "normal"
    warmup: str | None = None
    notes: list[str] = field(default_factory=list)
    gap_days: int | None = None
    day_id: int | None = None


def history(db: Db, eid: int) -> list[Perf]:
    perfs: list[Perf] = []
    cur_sid = None
    for r in db.exercise_sets(eid):
        if r["session_id"] != cur_sid:
            cur_sid = r["session_id"]
            target = None
            if r["program_id"] is not None and r["ord"] is not None:
                target = db.item_range(r["program_id"], r["ord"], eid)
            rir = r["rir"] if r["rir"] is not None else r["session_rir"]
            perfs.append(Perf(
                day=date.fromisoformat(r["day"]), reps=[], seconds=[], weights=[], kind=r["weight_kind"],
                rir=rir if rir is not None else DEFAULT_RIR, target=target, hint=r["hint"], hint_kg=r["hint_kg"],
                deload=is_deload(r["session_rir"], r["session_phase"]),
            ))
        p = perfs[-1]
        p.reps.append(r["reps"])
        p.seconds.append(r["seconds"])
        p.weights.append(r["weight"])
        if r["weight_kind"] != "none":
            p.kind = r["weight_kind"]
        p.hint = p.hint or r["hint"]
        p.hint_kg = p.hint_kg or r["hint_kg"]
    return perfs


def _scheme(item, rec: Rec, sets: int | None = None) -> str:
    sets = sets or item["sets"] or 1
    if item["seconds"]:
        secs = rec.seconds or item["reps_min"]
        return f"{sets}x{secs}с" if secs else f"{sets}xмакс"
    if item["to_max"] or item["reps_min"] is None:
        return f"{sets}xмакс"
    if item["reps_min"] == item["reps_max"]:
        return f"{sets}x{item['reps_min']}"
    return f"{sets}x{item['reps_min']}-{item['reps_max']}"


def dumbbell_steps(db: Db) -> list[float] | None:
    raw = db.get("dumbbell_steps")
    return [float(x.replace(",", ".")) for x in raw.split()] if raw else None


def bar_weight(db: Db) -> float:
    return float(db.get("bar_weight") or 20)


def target_for(item, day) -> Target:
    rir = day["rir"] if day["rir"] is not None else DEFAULT_RIR
    pct = None
    if m := PCT_RE.search(day["phase"] or ""):
        pct = (int(m["a"]) + int(m["b"] or m["a"])) / 200
    return Target(item["sets"], item["reps_min"], item["reps_max"], rir, bool(item["seconds"]), bool(item["to_max"]),
                  is_deload(day["rir"], day["phase"]), pct)


def gap_days(db: Db, today: date) -> int | None:
    last = db.last_session_day()
    return (today - date.fromisoformat(last)).days if last else None


def build_plan(db: Db, today: date, day=None, mode: str = "normal") -> Plan | None:
    day = day or program.next_day(db)
    if day is None:
        return None
    bw = db.bodyweight()
    steps = dumbbell_steps(db)
    avoid = state.avoided(db)
    items = db.day_items(day["id"])
    if mode == "short":
        items = [it for it in items if it["exercise_id"] not in avoid]
        items = items[: max(2, (len(items) + 1) // 2)]
    lines = []
    for it in items:
        t = target_for(it, day)
        sets = max(1, (it["sets"] or 1) - 1) if mode == "light" else None
        eid, name, equipment, prefix = it["exercise_id"], it["name"], it["equipment"], ""
        if eid in avoid:
            alts = movement.alternatives(db, eid, exclude=avoid, limit=1)
            if not alts:
                lines.append(PlanLine(name, _scheme(it, Rec(None, "none", "")), Rec(None, "none", "в /avoid — пропусти"), skip=True))
                continue
            eid, name = alts[0]
            equipment = db.exercise(eid)["equipment"]
            prefix = f"вместо «{it['name']}» · "
        rec = recommend(t, history(db, eid), today, equipment, bw, steps)
        if mode == "light" and rec.weight is not None:
            rec.weight = round_weight(rec.weight * LIGHT_FACTOR, equipment, rec.kind, steps, mode="down")
            rec.reason += f" · лёгкий день −{round((1 - LIGHT_FACTOR) * 100)}%"
        rec.reason = prefix + rec.reason
        lines.append(PlanLine(name, _scheme(it, rec, sets), rec, eid=eid, sets=sets or it["sets"]))

    plan = Plan(day["week_title"], day["title"], lines, mode, day_id=day["id"])
    equipment_of = {ln.eid: (db.exercise(ln.eid) or {"equipment": None})["equipment"] for ln in lines if ln.eid}
    first_bar = next((ln for ln in lines if not ln.skip and ln.rec.kind == "abs"
                      and equipment_of.get(ln.eid) == "barbell" and (ln.rec.weight or 0) >= 40), None)
    if first_bar:
        plan.warmup = f"Разминка ({first_bar.name}): {plates.warmup_text(first_bar.rec.weight, bar_weight(db))}"
    gap = gap_days(db, today)
    plan.gap_days = gap
    if gap is not None and gap > RESTART_AFTER_DAYS:
        plan.notes.append(f"Перерыв {gap} дн.: веса уже снижены. Начать цикл с первой недели — /restart.")
    elif gap is not None and gap_factor(gap) < 1:
        plan.notes.append(f"Перерыв {gap} дн.: веса снижены, не гонись за прошлыми.")
    if state.is_paused(db, today):
        plan.notes.append("Сейчас пауза. Запиши тренировку — и пауза снимется сама.")
    return plan


def _header(plan: Plan) -> list[str]:
    title = plan.title + (f" · {MODES[plan.mode]}" if MODES.get(plan.mode) else "")
    return [h for h in (plan.week_title, title) if h]


def render_text(plan: Plan) -> str:
    body = [ln.text for ln in plan.lines if not ln.skip]
    why = [f"• {ln.name}: {ln.rec.reason}" for ln in plan.lines]
    extra = ([plan.warmup] if plan.warmup else []) + plan.notes
    return "\n".join([*_header(plan), "", *body, "", *extra, "Почему:", *why])


def render_html(plan: Plan) -> str:
    """Copyable block in the owner's own format, reasons below it."""
    block = "\n".join([*_header(plan), "", *(ln.text for ln in plan.lines if not ln.skip)])
    parts = [f"<pre>{html.escape(block)}</pre>"]
    if plan.warmup:
        parts.append(f"<i>{html.escape(plan.warmup)}</i>")
    parts.append("\n".join(f"• <b>{html.escape(ln.name)}</b>: {html.escape(ln.rec.reason)}" for ln in plan.lines))
    parts += [f"⚠️ {html.escape(n)}" for n in plan.notes]
    return "\n".join(parts)


def swap_text(db: Db, eid: int, today: date) -> str:
    """Replacements for an exercise with weights, using today's scheme when it is in the plan."""
    ex = db.exercise(eid)
    day = program.next_day(db)
    item = next((it for it in db.day_items(day["id"]) if it["exercise_id"] == eid), None) if day else None
    t = target_for(item, day) if item else Target(3, 8, 12, DEFAULT_RIR)
    alts = movement.alternatives(db, eid, exclude=state.avoided(db))
    if not alts:
        group = movement.group_of(ex["name"])
        return (f"Для «{ex['name']}» замен в истории нет"
                + (f" (группа «{group}»)." if group else ".") + " Возьми похожее движение и запиши как есть.")
    lines = [f"Вместо «{ex['name']}»:"]
    for aid, name in alts:
        a = db.exercise(aid)
        rec = recommend(t, history(db, aid), today, a["equipment"], db.bodyweight(), dumbbell_steps(db))
        w = fmt_weight(rec.weight, rec.kind)
        lines.append(f"• {name}{' — ' + w if w else ''} ({rec.reason})")
    return "\n".join(lines)


def last_sessions_text(db: Db, eid: int, n: int = 5) -> str:
    rows = [f"{p.day:%d.%m.%y} · {p.done()}" for p in history(db, eid)[:n]]
    return "\n".join(rows) or "записей нет"
