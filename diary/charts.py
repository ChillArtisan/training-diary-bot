"""PNG charts. Pure drawing: every function takes plain data and returns image bytes.

Uses matplotlib's object API (no pyplot), so charts can be rendered in a worker thread.
"""
from __future__ import annotations

import io
from datetime import date, timedelta

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.dates import DateFormatter
from matplotlib.figure import Figure

ACCENT = "#2f81f7"
GOOD = "#2da44e"
WARN = "#d29922"
BAD = "#cf222e"
MUTED = "#8c959f"
LIGHT = "#eaeef2"
TEXT = "#1f2328"
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _figure(w: float = 8, h: float = 4.5) -> tuple[Figure, object]:
    fig = Figure(figsize=(w, h), dpi=150, facecolor="white")
    ax = fig.add_subplot()
    ax.set_facecolor("white")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(LIGHT)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.grid(axis="y", color=LIGHT, linewidth=0.8)
    ax.set_axisbelow(True)
    return fig, ax


def _title(ax, title: str, subtitle: str | None = None) -> None:
    ax.set_title(title, loc="left", fontsize=13, fontweight="bold", color=TEXT, pad=22 if subtitle else 10)
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9, color=MUTED)


def _png(fig: Figure) -> bytes:
    FigureCanvasAgg(fig)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.25)
    return buf.getvalue()


def _num(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def strength(name: str, points: list[tuple[date, float, float | None]]) -> bytes | None:
    """points: (day, estimated 1RM, top working weight)."""
    if len(points) < 2:
        return None
    fig, ax = _figure()
    days = [p[0] for p in points]
    e1 = [p[1] for p in points]
    best, prs = 0.0, []
    for d, e in zip(days, e1):
        if e > best:
            if best:
                prs.append((d, e))
            best = e
    ax.plot(days, e1, color=ACCENT, linewidth=2, marker="o", markersize=3.5, label="сила (оценка 1ПМ)")
    tops = [(d, w) for d, _, w in points if w]
    if tops:
        ax.plot([d for d, _ in tops], [w for _, w in tops], color=MUTED, linewidth=1, linestyle="--", label="рабочий вес")
    if prs:
        ax.scatter([d for d, _ in prs], [e for _, e in prs], marker="*", s=90, color=WARN, zorder=5, label="рекорд")
    ax.annotate(f"{_num(e1[-1])} кг", (days[-1], e1[-1]), xytext=(6, 0), textcoords="offset points",
                fontsize=9, color=ACCENT, va="center")
    ax.xaxis.set_major_formatter(DateFormatter("%d.%m.%y"))
    ax.set_ylabel("кг", color=MUTED, fontsize=9)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    change = e1[-1] - e1[0]
    _title(ax, name, f"лучшее {_num(max(e1))} кг · с начала {'+' if change >= 0 else ''}{_num(change)} кг · "
                     f"рекордов {len(prs)}")
    fig.autofmt_xdate()
    return _png(fig)


def consistency(training_days: set[date], planned_weekdays: set[int], today: date, weeks: int = 26,
                subtitle: str = "", plan_from: date | None = None) -> bytes:
    """GitHub-style calendar: a column per week, a row per weekday. Missed plan days are red only
    from `plan_from` (when the current program started): older weeks had other programs."""
    fig, ax = _figure(8, 3)
    ax.grid(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_visible(False)
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    trained = missed = 0
    for col in range(weeks):
        for row in range(7):
            d = start + timedelta(weeks=col, days=row)
            if d > today:
                continue
            if d in training_days:
                color = GOOD
                trained += 1
            elif row in planned_weekdays and (plan_from is None or d >= plan_from) and d < today:
                color = "#ffd8d3"
                missed += 1
            else:
                color = LIGHT
            ax.add_patch(_square(col, 6 - row, color))
        first_of_month = next((start + timedelta(weeks=col, days=r) for r in range(7)
                               if (start + timedelta(weeks=col, days=r)).day == 1), None)
        if first_of_month:
            ax.text(col + 0.5, 7.3, MONTHS[first_of_month.month - 1], fontsize=8, color=MUTED, ha="center")
    ax.set_xlim(-0.2, weeks + 0.2)
    ax.set_ylim(-0.2, 7.8)
    ax.set_yticks([6 - i + 0.45 for i in range(7)], WEEKDAYS)
    ax.set_xticks([])
    ax.set_aspect("equal")
    ax.tick_params(length=0)
    _title(ax, "Постоянство", subtitle or f"тренировок {trained} · пропущено плановых дней {missed}")
    return _png(fig)


def _square(x: float, y: float, color: str):
    from matplotlib.patches import FancyBboxPatch
    return FancyBboxPatch((x + 0.08, y + 0.08), 0.84, 0.84, boxstyle="round,pad=0,rounding_size=0.15",
                          linewidth=0, facecolor=color)


def weekly(weeks: list[tuple[date, int, float]], planned: int) -> bytes | None:
    """weeks: (monday, training days, tonnage in tonnes)."""
    if not weeks:
        return None
    fig, ax = _figure()
    xs = list(range(len(weeks)))
    colors = [GOOD if n >= planned else WARN if n >= planned - 1 and n else LIGHT for _, n, _ in weeks]
    colors[-1] = "#b6d4fb"  # the current week is still in progress
    ax.bar(xs, [n for _, n, _ in weeks], color=colors, width=0.65, label="тренировок")
    ax.text(xs[-1], weeks[-1][1] + 0.08, "идёт", color=ACCENT, fontsize=8, ha="center")
    ax.axhline(planned, color=MUTED, linestyle="--", linewidth=1)
    ax.text(len(weeks) - 0.5, planned + 0.08, f"план {planned}", color=MUTED, fontsize=8, ha="right")
    ax.set_xticks(xs, [f"{w:%d.%m}" for w, _, _ in weeks], rotation=45, fontsize=8)
    ax.set_ylim(0, max(planned, max(n for _, n, _ in weeks)) + 1)
    ax.set_ylabel("тренировок", color=MUTED, fontsize=9)
    ton = ax.twinx()
    ton.plot(xs, [t for _, _, t in weeks], color=ACCENT, marker="o", markersize=3, linewidth=1.5)
    ton.set_ylabel("тоннаж, т", color=ACCENT, fontsize=9)
    ton.tick_params(colors=ACCENT, labelsize=8)
    for side in ("top", "left", "bottom"):
        ton.spines[side].set_visible(False)
    ton.spines["right"].set_color(LIGHT)
    ton.set_ylim(bottom=0)
    _title(ax, "Недели", "столбцы — тренировки, линия — тоннаж")
    return _png(fig)


def kpd(points: list[tuple[date, float]]) -> bytes | None:
    if len(points) < 2:
        return None
    fig, ax = _figure()
    xs = list(range(len(points)))
    vals = [v for _, v in points]
    colors = [GOOD if v >= 85 else WARN if v >= 65 else BAD for v in vals]
    ax.scatter(xs, vals, c=colors, s=28, zorder=3)
    window = 5
    avg = [sum(vals[max(0, i - window + 1): i + 1]) / len(vals[max(0, i - window + 1): i + 1]) for i in xs]
    ax.plot(xs, avg, color=ACCENT, linewidth=2, label=f"среднее за {window}")
    ax.set_ylim(0, 105)
    step = max(1, len(points) // 10)
    ax.set_xticks(xs[::step], [f"{d:%d.%m}" for d, _ in points][::step], rotation=45, fontsize=8)
    ax.set_ylabel("%", color=MUTED, fontsize=9)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    _title(ax, "КПД тренировок", "выполнение плана · подходы в цели · прибавка к прошлому разу")
    return _png(fig)


def volume(sets_per_week: dict[str, float], zone: tuple[int, int] = (10, 20)) -> bytes | None:
    if not sets_per_week:
        return None
    items = sorted(sets_per_week.items(), key=lambda x: x[1])
    fig, ax = _figure(8, max(3, 0.32 * len(items) + 1))
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=LIGHT, linewidth=0.8)
    ax.axvspan(*zone, color=GOOD, alpha=0.08, linewidth=0)
    ys = list(range(len(items)))
    colors = [GOOD if zone[0] <= v <= zone[1] else WARN if v > zone[1] else ACCENT for _, v in items]
    ax.barh(ys, [v for _, v in items], color=colors, height=0.6)
    ax.set_yticks(ys, [g for g, _ in items], fontsize=9)
    for y, (_, v) in zip(ys, items):
        ax.text(v + 0.2, y, _num(v), va="center", fontsize=8, color=MUTED)
    ax.set_xlabel("рабочих подходов в неделю", color=MUTED, fontsize=9)
    _title(ax, "Объём по мышцам", f"последние 4 недели · зелёная зона {zone[0]}–{zone[1]} подходов — ориентир для роста")
    return _png(fig)


def bodyweight(points: list[tuple[date, float]]) -> bytes | None:
    if len(points) < 2:
        return None
    fig, ax = _figure()
    days = [d for d, _ in points]
    vals = [v for _, v in points]
    ax.scatter(days, vals, color=MUTED, s=14, zorder=3)
    avg = []
    for i, d in enumerate(days):
        recent = [v for d2, v in points[: i + 1] if (d - d2).days < 7]
        avg.append(sum(recent) / len(recent))
    ax.plot(days, avg, color=ACCENT, linewidth=2, label="среднее за 7 дней")
    ax.xaxis.set_major_formatter(DateFormatter("%d.%m.%y"))
    ax.set_ylabel("кг", color=MUTED, fontsize=9)
    ax.legend(frameon=False, fontsize=8)
    change = avg[-1] - avg[0]
    _title(ax, "Вес тела", f"сейчас {_num(vals[-1])} кг · с начала {'+' if change >= 0 else ''}{_num(change)} кг")
    fig.autofmt_xdate()
    return _png(fig)
