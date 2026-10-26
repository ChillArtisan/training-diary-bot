"""Weight recommendation. Pure functions; every tunable number is here."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

DEFAULT_RIR = 2.0          # sessions logged without a RIR header
E1RM_WINDOW_DAYS = 42      # sessions used for the strength estimate
MAX_STEP_UP = 1.10         # clamp vs the last weight when the scheme changes
MAX_STEP_DOWN = 0.90
DELOAD_FLOOR = 0.50
DELOAD_RIR = 4.0
SNAP_KG = 1.5              # machines: snap to a stack weight used before if this close
MISS_CUT = 0.95            # second miss in a row at the same weight
SECONDS_STEP = 5
GAP_PENALTY = [(60, 0.70), (30, 0.80), (14, 0.90)]  # (more than N days, factor)

INCREMENT = {"barbell": 2.5, "machine": 2.5, "cable": 2.5, "bodyweight": 2.5}
DUMBBELLS = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 12.5, 14, 15, 16, 17.5, 18, 20, 22, 22.5, 23, 24, 25,
             26, 27.5, 28, 30, 32, 32.5, 34, 35, 36, 37.5, 40, 42.5, 45, 47.5, 50]


@dataclass
class Target:
    sets: int | None
    reps_min: int | None
    reps_max: int | None
    rir: float
    seconds: bool = False
    to_max: bool = False
    deload: bool = False


@dataclass
class Perf:
    """One past session of an exercise."""
    day: date
    reps: list[int | None]
    seconds: list[int | None]
    weights: list[float | None]
    kind: str                    # abs | added | units | smith | none
    rir: float                   # logged @RIR, else the session's target, else DEFAULT_RIR
    target: tuple[int, int] | None = None  # rep range of the matched program item
    hint: str | None = None
    hint_kg: float | None = None

    @property
    def top(self) -> float | None:
        ws = [w for w in self.weights if w is not None]
        return max(ws) if ws else None

    def done(self) -> str:
        vals = [s for s in self.seconds if s] if any(self.seconds) else [r for r in self.reps if r]
        unit = "с" if any(self.seconds) else ""
        if not vals:
            body = f"{len(self.reps)}x?"
        elif len(set(vals)) == 1:
            body = f"{len(vals)}x{vals[0]}{unit}"
        else:
            body = "/".join(f"{v}{unit}" for v in vals)
        top = self.top
        if top is None:
            return body
        return f"{body} {fmt_weight(top, self.kind)}"


@dataclass
class Rec:
    weight: float | None
    kind: str
    reason: str
    seconds: int | None = None
    notes: list[str] = field(default_factory=list)


def fmt_num(w: float) -> str:
    return f"{w:g}".replace(".", ",")


def fmt_weight(w: float | None, kind: str) -> str:
    if w is None:
        return ""
    if kind == "added":
        return f"+{fmt_num(w)}кг"
    if kind == "smith":
        return f"смит+{fmt_num(w)}кг"
    if kind == "units":
        return f"{int(w)} плитка"
    return f"{fmt_num(w)}кг"


def e1rm(weight: float, reps: int, rir: float) -> float:
    """Epley with reps in reserve."""
    return weight * (1 + (reps + rir) / 30)


def weight_for(e1: float, reps: int, rir: float) -> float:
    return e1 / (1 + (reps + rir) / 30)


def round_weight(w: float, equipment: str, kind: str, dumbbells: list[float] | None = None,
                 used: set[float] | None = None) -> float:
    if kind == "units":
        return float(max(1, round(w)))
    if equipment == "dumbbell" and kind == "abs":
        steps = dumbbells or DUMBBELLS
        return min(steps, key=lambda s: (abs(s - w), -s))
    if equipment in ("machine", "cable") and kind == "abs" and used:
        near = min(used, key=lambda u: abs(u - w))
        if abs(near - w) <= SNAP_KG:
            return near  # stacks have odd plates (41, 45.4, 53.25): reuse a weight that exists
    return round(w / 2.5) * 2.5


def step_up(w: float, equipment: str, kind: str, dumbbells: list[float] | None = None) -> float:
    if kind == "units":
        return w + 1
    if equipment == "dumbbell" and kind == "abs":
        steps = dumbbells or DUMBBELLS
        return next((s for s in sorted(steps) if s > w), w + 2.5)
    return w + INCREMENT.get(equipment, 2.5)


def gap_factor(days: int) -> float:
    return next((f for limit, f in GAP_PENALTY if days > limit), 1.0)


def _missed(p: Perf) -> bool:
    lo = p.target[0] if p.target else None
    reps = [r for r in p.reps if r is not None]
    return bool(lo and reps and min(reps) < lo)


def recommend(t: Target, history: list[Perf], today: date, equipment: str,
              bodyweight: float | None = None, dumbbells: list[float] | None = None) -> Rec:
    """history: newest first."""
    if not history:
        return Rec(None, "none", "новое — подбери вес")
    last = history[0]
    gap = (today - last.day).days
    factor = gap_factor(gap)
    gap_note = f" · пауза {gap} дн → −{round((1 - factor) * 100)}%" if factor < 1 else ""
    was = f"было {last.done()}"

    if t.seconds or any(last.seconds):
        best = max((s for s in last.seconds if s), default=None)
        if best is None:
            return Rec(None, "none", was)
        goal = int(best * factor) if factor < 1 else best + SECONDS_STEP
        return Rec(None, "none", f"{was} · цель {goal}с{gap_note}", seconds=goal)

    w_last = last.top
    if w_last is None or last.kind == "none":
        return Rec(None, "none", was)
    kind = last.kind

    reps = [r for r in last.reps if r is not None]
    same_scheme = (
        last.target is not None and t.reps_min is not None
        and last.target == (t.reps_min, t.reps_max) and abs(last.rir - t.rir) < 0.6
    )

    if same_scheme and reps:
        lo, hi = last.target
        if min(reps) >= hi:
            w = step_up(w_last, equipment, kind, dumbbells)
            why = f"все по верху → +{fmt_num(w - w_last)}"
        elif min(reps) < lo:
            prev = history[1] if len(history) > 1 else None
            if prev and prev.top == w_last and _missed(prev):
                w, why = w_last * MISS_CUT, "второй недобор → −5%"
            else:
                w, why = w_last, "недобор → держим"
        else:
            w, why = w_last, "добирай повторы"
    elif kind == "units" or t.reps_min is None or not reps:
        w, why = w_last, "держим"
    else:
        add = (bodyweight or 0) if kind == "added" else 0
        recent = [p for p in history if (last.day - p.day).days <= E1RM_WINDOW_DAYS and p.kind == kind]
        # deload sets are rarely done at the logged RIR and would inflate the estimate
        recent = [p for p in recent if p.rir < DELOAD_RIR] or recent
        est = max(
            e1rm(wt + add, r, p.rir)
            for p in recent for r, wt in zip(p.reps, p.weights) if r and wt is not None
        )
        w = weight_for(est, t.reps_min, t.rir) - add
        low = DELOAD_FLOOR if t.deload else MAX_STEP_DOWN
        w = min(max(w, w_last * low), w_last * MAX_STEP_UP)
        why = "разгрузка" if t.deload else "новая схема, по силе"

    if last.hint == "hold":
        w, why = min(w, w_last), "пометка «не добавлять»"
    elif last.hint == "bump" and same_scheme:
        w, why = max(w, step_up(w_last, equipment, kind, dumbbells)), "пометка «можно добавить»"
    elif last.hint == "target" and last.hint_kg and same_scheme:
        w, why = last.hint_kg, f"пометка «{fmt_num(last.hint_kg)}»"

    used = {wt for p in history if p.kind == kind for wt in p.weights if wt is not None}
    w = round_weight(w * factor, equipment, kind, dumbbells, used)
    if kind == "added" and w <= 0:
        return Rec(None, "none", f"{was} · без веса{gap_note}")
    return Rec(w, kind, f"{was} · {why}{gap_note}")
