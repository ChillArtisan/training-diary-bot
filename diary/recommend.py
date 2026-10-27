"""Weight recommendation. Pure functions; every tunable number is here."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

DEFAULT_RIR = 2.0          # sessions logged without a RIR header
E1RM_WINDOW_DAYS = 42      # sessions used for the strength estimate
MAX_STEP_UP = 1.10         # never more than +10% over the last working weight ...
MAX_STEP_DOWN = 0.90       # ... nor below −10% when the scheme changes
RIR_TOLERANCE = 0.6        # «same scheme» if the RIR target moved less than this
DELOAD_RIR = 4.0
DELOAD_FACTOR = 0.85       # a deload is at most 85% of the last normal working weight
DELOAD_FLOOR = 0.50
SNAP_KG = 1.5              # machines: a stack weight used before counts as «that weight» within this
MISS_CUT = 0.95            # second miss in a row at the same weight
SECONDS_STEP = 5
GAP_PENALTY = [(60, 0.70), (30, 0.80), (14, 0.90)]  # (more than N days, factor)
LIGHT_FACTOR = 0.85        # the «light day» button
RESTART_AFTER_DAYS = 21    # a break this long: suggest starting the cycle over

INCREMENT = {"barbell": 2.5, "machine": 2.5, "cable": 2.5, "bodyweight": 2.5}
GRID = 2.5                 # plates: barbell, belt, Smith
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
    pct: float | None = None     # «~55-60% ПМ» in the week header -> 0.575


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
    deload: bool = False

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


def is_deload(rir: float | None, phase: str | None) -> bool:
    """A deload day: RIR 4+ or a phase named «разгрузка»/«deload»."""
    text = (phase or "").lower()
    return (rir is not None and rir >= DELOAD_RIR) or "разгруз" in text or "deload" in text


def e1rm(weight: float, reps: int, rir: float) -> float:
    """Epley with reps in reserve."""
    return weight * (1 + (reps + rir) / 30)


def weight_for(e1: float, reps: int, rir: float) -> float:
    return e1 / (1 + (reps + rir) / 30)


def candidates(equipment: str, kind: str, dumbbells: list[float] | None = None, used: set[float] | None = None,
               upto: float = 0) -> list[float]:
    """Weights that can actually be loaded, up to a bit above `upto`."""
    top = upto + 2 * GRID
    if kind == "units":
        return [float(n) for n in range(1, int(top) + 2)]
    if equipment == "dumbbell" and kind == "abs":
        steps = sorted(dumbbells or DUMBBELLS)
        w = steps[-1]
        while w < top:  # heavier than the rack list: 2,5 kg steps
            w += GRID
            steps.append(w)
        return steps
    grid = [i * GRID for i in range(0 if kind == "added" else 1, int(top / GRID) + 2)]
    if equipment in ("machine", "cable") and kind == "abs" and used:
        # stacks have odd plates (41, 45.4, 53.25): a weight used before replaces the grid points near it
        grid = [g for g in grid if all(abs(g - u) > SNAP_KG for u in used)] + list(used)
    return sorted(set(grid))


def pick(target: float, options: list[float], mode: str = "near", cap: float | None = None) -> float:
    """near: the closest (ties go down); down: the heaviest not above target; up: the lightest not below it.
    Never above cap."""
    eps = 1e-9
    opts = [o for o in options if cap is None or o <= cap + eps] or [min(options)]
    if mode == "down":
        below = [o for o in opts if o <= target + eps]
        return max(below) if below else min(opts)
    if mode == "up":
        above = [o for o in opts if o >= target - eps]
        return min(above) if above else max(opts)
    return min(opts, key=lambda o: (abs(o - target), o))


def round_weight(w: float, equipment: str, kind: str, dumbbells: list[float] | None = None,
                 used: set[float] | None = None, mode: str = "near", cap: float | None = None) -> float:
    return pick(w, candidates(equipment, kind, dumbbells, used, max(w, cap or 0)), mode, cap)


def step_up(w: float, equipment: str, kind: str, dumbbells: list[float] | None = None,
            used: set[float] | None = None) -> float:
    """The next loadable weight above w: one increment at most (dumbbells go to the next pair)."""
    if kind == "units":
        return w + 1
    opts = candidates(equipment, kind, dumbbells, used, w + GRID)
    if equipment == "dumbbell" and kind == "abs":
        return next((o for o in opts if o > w + 1e-9), w + GRID)
    cap = w + INCREMENT.get(equipment, GRID)
    above = [o for o in opts if w + 1e-9 < o <= cap + 1e-9]
    return min(above) if above else cap


def gap_factor(days: int) -> float:
    return next((f for limit, f in GAP_PENALTY if days > limit), 1.0)


def _missed(p: Perf) -> bool:
    lo = p.target[0] if p.target else None
    reps = [r for r in p.reps if r is not None]
    return bool(lo and reps and min(reps) < lo)


def _estimate(history: list[Perf], since: Perf, kind: str, add: float) -> float | None:
    """The best e1RM of the recent sessions."""
    recent = [p for p in history if (since.day - p.day).days <= E1RM_WINDOW_DAYS and p.kind == kind]
    return max(
        (e1rm(wt + add, r, p.rir) for p in recent for r, wt in zip(p.reps, p.weights) if r and wt is not None),
        default=None,
    )


def recommend(t: Target, history: list[Perf], today: date, equipment: str,
              bodyweight: float | None = None, dumbbells: list[float] | None = None) -> Rec:
    """history: newest first. Progression goes from the last normal session, never from a deload."""
    if not history:
        return Rec(None, "none", "новое — подбери вес")
    normal = [p for p in history if not (p.deload or p.rir >= DELOAD_RIR)]
    base = normal[0] if normal else history[0]
    gap = (today - history[0].day).days
    factor = gap_factor(gap)
    gap_note = f" · пауза {gap} дн → −{round((1 - factor) * 100)}%" if factor < 1 else ""
    was = f"было {base.done()}" + (" до разгрузки" if base is not history[0] else "")

    if t.seconds or any(base.seconds):
        best = max((s for s in base.seconds if s), default=None)
        if best is None:
            return Rec(None, "none", was)
        if factor < 1:
            goal = int(best * factor)
        elif t.deload or base.hint == "hold":
            goal = best
        else:
            goal = best + SECONDS_STEP
        return Rec(None, "none", f"{was} · цель {goal}с{gap_note}", seconds=goal)

    w_last = base.top
    if w_last is None or base.kind == "none":
        return Rec(None, "none", was)
    kind = base.kind
    add = (bodyweight or 0) if kind == "added" else 0
    used = {wt for p in history if p.kind == kind for wt in p.weights if wt is not None}
    reps = [r for r in base.reps if r is not None]
    same_scheme = (
        base.target is not None and t.reps_min is not None
        and base.target == (t.reps_min, t.reps_max) and abs(base.rir - t.rir) < RIR_TOLERANCE
    )
    cap = max(w_last * MAX_STEP_UP, step_up(w_last, equipment, kind, dumbbells, used))
    mode = "down"  # holding or lowering never rounds up

    if t.deload:
        est = _estimate(normal or history, base, kind, add) if t.reps_min else None
        options = [w_last * DELOAD_FACTOR]
        if est:
            options.append(weight_for(est, t.reps_min, t.rir) - add)
            if t.pct:
                options.append(est * t.pct - add)
        w, why, cap = max(min(options), w_last * DELOAD_FLOOR), "разгрузка", w_last
    elif same_scheme and reps:
        lo, hi = base.target
        if min(reps) >= hi:
            w, why, mode = step_up(w_last, equipment, kind, dumbbells, used), "все по верху", "up"
        elif min(reps) < lo:
            prev = normal[1] if len(normal) > 1 else None
            if prev and prev.top == w_last and _missed(prev):
                w, why, mode, cap = w_last * MISS_CUT, "второй недобор", "near", w_last - 1e-6  # at least a step
            else:
                w, why = w_last, "недобор → держим"
        else:
            w, why = w_last, "добирай повторы"
    elif kind == "units" or t.reps_min is None or not reps:
        w, why = w_last, "держим"
    elif (est := _estimate(normal or history, base, kind, add)) is None:
        w, why = w_last, "держим"
    else:
        w = weight_for(est, t.reps_min, t.rir) - add
        w = min(max(w, w_last * MAX_STEP_DOWN), w_last * MAX_STEP_UP)
        why, mode, cap = "новая схема, по силе", "near", w_last * MAX_STEP_UP

    if base.hint == "hold":
        w, why, mode, cap = min(w, w_last), "пометка «не добавлять»", "down", w_last
    elif base.hint == "bump" and same_scheme and not t.deload:
        w, why, mode = max(w, step_up(w_last, equipment, kind, dumbbells, used)), "пометка «можно добавить»", "up"
    elif base.hint == "target" and base.hint_kg and same_scheme and not t.deload:
        w = min(max(base.hint_kg, w_last * MAX_STEP_DOWN), cap)
        why = f"пометка «{fmt_num(base.hint_kg)}»" + (" — не больше +10% за раз" if w < base.hint_kg else "")
        mode = "near"

    if factor < 1:
        w, mode = w * factor, "down"
    w = round_weight(w, equipment, kind, dumbbells, used, mode, cap)
    if kind == "added" and w <= 0:
        return Rec(None, "none", f"{was} · без веса{gap_note}")
    if why in ("все по верху", "второй недобор", "разгрузка") and w != w_last:
        why += f" → {'+' if w > w_last else '−'}{fmt_num(round(abs(w - w_last), 2))}"
    return Rec(w, kind, f"{was} · {why}{gap_note}")
