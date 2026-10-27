"""Owner-editable settings with defaults and validation."""
from __future__ import annotations

import re

from diary.db import Db

ON_OFF = r"on|off"
TIME = r"([01]?\d|2[0-3]):[0-5]\d"
NUMBER = r"\d+(?:[.,]\d+)?"
NUMBERS = rf"{NUMBER}(?:\s+{NUMBER})*"

# key -> (default, pattern, description)
PREFS: dict[str, tuple[str, str, str]] = {
    "reminder": ("on", ON_OFF, "утром присылать программу в канал"),
    "reminder_time": ("08:00", TIME, "время утренней программы, МСК"),
    "nudge": ("on", ON_OFF, "вечером спросить, если тренировки не было"),
    "nudge_time": ("21:30", TIME, "время вечернего вопроса"),
    "summary": ("on", ON_OFF, "итоги недели в воскресенье"),
    "summary_time": ("20:00", TIME, "время итогов недели"),
    "monthly": ("on", ON_OFF, "1-го числа отчёт за месяц с графиками"),
    "log_feedback": ("on", ON_OFF, "после записи тренировки присылать её КПД"),
    "log_settle": ("40", r"\d{1,3}", "через сколько минут без правок пост тренировки считается законченным"),
    "backup_send": ("on", ON_OFF, "раз в неделю присылать копию базы"),
    "bodyweight": ("", NUMBER, "вес тела, если нет постов вида «70,6кг»"),
    "bar_weight": ("20", NUMBER, "вес грифа"),
    "dumbbell_steps": ("", NUMBERS, "гантели в зале через пробел"),
}


def get(db: Db, key: str) -> str:
    return db.get(key) or PREFS[key][0]


def on(db: Db, key: str) -> bool:
    return get(db, key) == "on"


def hhmm(db: Db, key: str) -> tuple[int, int]:
    h, m = get(db, key).split(":")
    return int(h), int(m)


def validate(key: str, value: str) -> str | None:
    """Normalized value, or None when it does not fit."""
    if key not in PREFS:
        return None
    value = value.strip().lower().replace(",", ".")
    return value if re.fullmatch(PREFS[key][1], value) else None
