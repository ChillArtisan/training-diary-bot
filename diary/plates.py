"""Barbell helpers: plates per side and a warm-up ramp."""
from __future__ import annotations

from .recommend import fmt_num

PLATES = [25, 20, 15, 10, 5, 2.5, 1.25]
RAMP = [(None, 10), (0.4, 5), (0.6, 3), (0.8, 2)]  # (share of the work weight, reps); None = empty bar


def plates_per_side(total: float, bar: float = 20, plates: list[float] | None = None) -> tuple[list[float], float]:
    """Greedy plates for one side -> (plates, the total actually loaded)."""
    side = max(0.0, (total - bar) / 2)
    out = []
    for p in sorted(plates or PLATES, reverse=True):
        while side + 1e-9 >= p:
            out.append(p)
            side -= p
    return out, bar + 2 * sum(out)


def plates_text(total: float, bar: float = 20, plates: list[float] | None = None) -> str:
    if total < bar:
        return f"Это меньше грифа ({fmt_num(bar)} кг)."
    side, loaded = plates_per_side(total, bar, plates)
    if not side:
        return f"Пустой гриф {fmt_num(bar)} кг."
    text = f"{fmt_num(total)} кг: по {' + '.join(fmt_num(p) for p in side)} на сторону (гриф {fmt_num(bar)})"
    if abs(loaded - total) > 1e-9:
        text += f"\nТочно не собрать, ближайшее — {fmt_num(loaded)} кг."
    return text


def warmup(work: float, bar: float = 20) -> list[tuple[float, int]]:
    steps: list[tuple[float, int]] = []
    for share, reps in RAMP:
        w = bar if share is None else max(bar, round(work * share / 2.5) * 2.5)
        if w < work and (not steps or w > steps[-1][0]):
            steps.append((w, reps))
    return steps


def warmup_text(work: float, bar: float = 20) -> str:
    return " · ".join(("гриф" if w == bar else fmt_num(w)) + f"×{r}" for w, r in warmup(work, bar))
