"""Generate a synthetic Telegram channel export (result.json) to try the bot without a real diary.

    python examples/make_demo_export.py
    python -m diary --db demo.db import examples/demo_export
    python -m diary --db demo.db today
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))
START = datetime(2026, 9, 7, 18, 0, tzinfo=MSK)  # a Monday

PROGRAM = """НЕДЕЛЯ 1: ВВОД · RIR 3
ПОНЕДЕЛЬНИК: ВЕРХ
Жим штанги лёжа – 4x6-8
Тяга штанги в наклоне – 4x8-10
Махи гантелями в стороны – 3x12-15

ЧЕТВЕРГ: НИЗ
Приседания со штангой – 4x6-8
Румынская тяга – 3x8-10
Планка – 3x60с

НЕДЕЛЯ 2: ПИК · RIR 1
ПОНЕДЕЛЬНИК: ВЕРХ
Жим штанги лёжа – 4x5-6
Тяга штанги в наклоне – 4x6-8
Махи гантелями в стороны – 3x10-12

ЧЕТВЕРГ: НИЗ
Приседания со штангой – 4x5-6
Румынская тяга – 3x6-8
Планка – 3x60с"""

# (days after START, post text) — written the way people really write: typos, notes, partial sets
LOGS = [
    (3, "НЕДЕЛЯ 1: ВВОД · RIR 3\nЧЕТВЕРГ: НИЗ\nПриседания 4х8 70кг\nРумынская тяга 3х10 60кг\nПланка 3хмакс(60с; 55с; 50с)"),
    (7, "НЕДЕЛЯ 2: ПИК · RIR 1\nПОНЕДЕЛЬНИК: ВЕРХ\nЖим лежа 4х6 65кг (1-6; 2-6; 3-5; 4-5)\n"
        "Тяга в наклоне 4х8 62,5кг - можно добавить\nМахи в стороны 3х12 8кг"),
    (10, "ЧЕТВЕРГ: НИЗ\nПриседания 4х6 80кг\nРумынская тяга 3х8 70кг\nПланка 3хмакс(65с; 60с; 60с)"),
    (11, "76,8кг"),
    (14, "НЕДЕЛЯ 1: ВВОД · RIR 3\nПОНЕДЕЛЬНИК: ВЕРХ\nЖим штанги лёжа – 4x8 62,5кг\nТяга штанги в наклоне – 4x10 65кг\n"
         "Махи гантелями в стороны – 3x15 8кг\nСегодня отлично, жим шёл легко"),
    (18, "вчера\nЧЕТВЕРГ: НИЗ\nПриседания со штангой – 4x8 75кг @2\nРумынская тяга – 3x10 65кг - не добавлять"),
    (21, "НЕДЕЛЯ 2: ПИК · RIR 1\nПОНЕДЕЛЬНИК: ВЕРХ\nЖим штанги лёжа – 4x6 70кг\nТяга штанги в наклоне – 4x8 67,5кг\n"
         "Махи гантелями в стороны – 3x12 9кг"),
    (22, "Замеры\nВес: 77,1\nОбхват груди: 101\nОбхват талии: 79"),
]


def message(i: int, when: datetime, text: str) -> dict:
    return {"id": i, "type": "message", "date": when.replace(tzinfo=None).isoformat(),
            "date_unixtime": str(int(when.timestamp())), "from": "Gym diary", "text": text}


def build() -> dict:
    msgs = [message(1, START - timedelta(days=1), PROGRAM)]
    msgs += [message(i + 2, START + timedelta(days=d), text) for i, (d, text) in enumerate(LOGS)]
    return {"name": "Gym diary (demo)", "type": "private_channel", "id": 1000000001, "messages": msgs}


if __name__ == "__main__":
    out = Path(__file__).with_name("demo_export") / "result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(build(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out}")
