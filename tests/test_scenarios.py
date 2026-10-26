"""Real-life situations: late logs, split workouts, bad days, pain, breaks, records."""
from datetime import date, datetime, timedelta, timezone

import pytest

from diary import backup, movement, plates, program, state, stats
from diary.db import Db
from diary.ingest import ingest_post, reparse_all, resolve_day
from diary.parse import parse_post
from diary.today import build_plan, render_html, swap_text
from tests.test_flow import TEMPLATE

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def at(days, hours=0):
    return T0 + timedelta(days=days, hours=hours)


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "d.db")
    program.activate(d, ingest_post(d, 1, at(0), TEMPLATE).program_id)
    return d


def test_backdated_log():
    assert parse_post("вчера\nЖим лежа 3х8 60кг").date.days_back == 1
    p = parse_post("ВТОРНИК 27.10: НИЗ А\nПриседания 4х8 80кг")
    assert (p.blocks[0].label, p.blocks[0].date.day, p.blocks[0].date.month) == ("НИЗ А", 27, 10)
    hint = p.blocks[0].date
    assert resolve_day("2026-10-29", hint) == "2026-10-27"
    assert resolve_day("2026-01-03", parse_post("28.12\nЖим 3х8 50кг").date) == "2025-12-28"
    assert resolve_day("2026-10-29", parse_post("01.05\nЖим 3х8 50кг").date) == "2026-10-29"  # too old: ignored


def test_backdated_session_day(db):
    ingest_post(db, 2, at(3), "позавчера\nЖим штанги лежа – 4x8 60кг")
    assert db.last_session_day() == "2026-10-02"


def test_workout_posted_in_two_parts(db):
    ingest_post(db, 2, at(4), "ПОНЕДЕЛЬНИК: ВЕРХ А\nЖим штанги лежа – 4x8 60кг")
    ingest_post(db, 3, at(4, 2), "Тяга штанги в наклоне – 4x10 60кг")  # same day, no header
    assert program.next_day(db)["title"] == "ВТОРНИК: НИЗ А"


def test_measurements_post(db):
    r = ingest_post(db, 5, at(1), "Замеры на 01.10.2026\n\nВес: 70\nРост: 181\nОбхват груди: 95\nОбхват талии: 74")
    assert r.kind == "body"
    ingest_post(db, 6, at(30), "Замеры\nОбхват груди: 98\nОбхват талии: 75")
    text = stats.measurements_text(db)
    assert "обхват груди: 98 (+3)" in text


def test_light_and_short_days(db):
    ingest_post(db, 2, at(4), "НЕДЕЛЯ 1\nПОНЕДЕЛЬНИК: ВЕРХ А\nЖим штанги лежа – 4x8 60кг\nТяга штанги в наклоне – 4x10 60кг")
    ingest_post(db, 3, at(5), "ВТОРНИК: НИЗ А\nПриседания со штангой – 4x8 80кг\nПланка 3хмакс(70с; 60с; 67с)")
    normal = build_plan(db, date(2026, 10, 12))
    light = build_plan(db, date(2026, 10, 12), mode="light")
    short = build_plan(db, date(2026, 10, 12), mode="short")
    assert light.lines[0].rec.weight < normal.lines[0].rec.weight
    assert light.lines[0].scheme.startswith("4x")  # 5 sets in week 2 minus one
    assert len(short.lines) < len(normal.lines) or len(normal.lines) <= 2
    assert "лёгкий день" in render_html(light)


def test_avoid_replaces_with_known_alternative(db):
    ingest_post(db, 2, at(2), "Жим гантелей лёжа 3х10 24кг")
    ingest_post(db, 3, at(4), "НЕДЕЛЯ 1\nПОНЕДЕЛЬНИК: ВЕРХ А\nЖим штанги лежа – 4x8 60кг\nТяга штанги в наклоне – 4x10 60кг")
    ingest_post(db, 4, at(5), "ВТОРНИК: НИЗ А\nПриседания со штангой – 4x8 80кг")
    bench = db.exercise_by_alias("жим штанги лежа")
    state.avoid(db, bench)
    plan = build_plan(db, date(2026, 10, 12))
    assert plan.lines[0].name == "Жим гантелей лёжа" and "вместо" in plan.lines[0].rec.reason
    assert "Жим гантелей лёжа" in swap_text(db, bench, date(2026, 10, 12))
    state.allow(db, bench)
    assert build_plan(db, date(2026, 10, 12)).lines[0].name == "Жим штанги лежа"


def test_pause_ends_with_a_logged_workout(db):
    state.pause(db, date(2026, 10, 10), None)
    assert state.is_paused(db, date(2026, 10, 15))
    r = ingest_post(db, 2, at(20), "Жим штанги лежа – 4x8 50кг")
    assert r.resumed and not state.is_paused(db, date(2026, 10, 22))


def test_old_posts_do_not_end_a_pause(db):
    ingest_post(db, 2, at(2), "Жим штанги лежа – 4x8 60кг")
    state.pause(db, date(2026, 10, 10), None)
    reparse_all(db)
    assert state.is_paused(db, date(2026, 10, 15))


def test_long_break_suggests_restart(db):
    ingest_post(db, 2, at(4), "НЕДЕЛЯ 1\nПОНЕДЕЛЬНИК: ВЕРХ А\nЖим штанги лежа – 4x8 60кг")
    plan = build_plan(db, date(2026, 12, 1))
    assert any("/restart" in n for n in plan.notes)
    assert program.restart(db)["ord"] == 0


def test_records(db):
    ingest_post(db, 2, at(2), "Жим штанги лежа – 3x8 60кг")
    first = ingest_post(db, 3, at(5), "Жим штанги лежа – 3x8 62,5кг")
    assert first.records and first.records[0].exercise == "Жим штанги лёжа"
    assert not ingest_post(db, 4, at(8), "Жим штанги лежа – 3x6 60кг").records
    assert "Жим штанги лёжа" in " ".join(stats.records_between(db, date(2026, 10, 1), date(2026, 10, 31)))


def test_reparse_is_idempotent(db):
    ingest_post(db, 2, at(4), "НЕДЕЛЯ 1\nПОНЕДЕЛЬНИК: ВЕРХ А\nЖим штанги лежа – 4x8 60кг")
    before = (db.counts(), program.next_day(db)["ord"], db.active_program()["post_id"])
    reparse_all(db)
    assert (db.counts(), program.next_day(db)["ord"], db.active_program()["post_id"]) == before


def test_backup_and_export(db):
    ingest_post(db, 2, at(2), "Жим штанги лежа – 3x8 60кг")
    for i in range(16):
        backup.make_backup(db, date(2026, 10, 1) + timedelta(days=i))
    assert len(list(backup.backup_dir(db).glob("*.db"))) == backup.KEEP
    restored = Db(backup.latest_backup(db))
    assert restored.counts()["sets"] == 3
    assert "Жим штанги лёжа" in backup.export_csv(db).decode("utf-8-sig")


def test_plates_and_warmup():
    assert plates.plates_per_side(82.5) == ([25, 5, 1.25], 82.5)
    assert plates.plates_per_side(61)[1] == 60
    assert plates.warmup(100) == [(20, 10), (40, 5), (60, 3), (80, 2)]
    assert "гриф×10" in plates.warmup_text(100)


def test_movement_groups():
    assert movement.group_of("Жим гантелей на наклонной 30°") == "жим на наклонной"
    assert movement.group_of("Тяга лица (Face Pull)") == "задняя дельта"
    assert movement.group_of("Разгибания ног сидя") == "разгибание ног"
    assert movement.group_of("Разгибания на трицепс в блоке") == "трицепс"
    assert movement.group_of("Подъём штанги на бицепс") == "бицепс"
    assert movement.group_of("Подъём на носки стоя") == "икры"
    assert movement.group_of("Тяга верхнего блока U-хватом") == "вертикальная тяга"
    assert movement.group_of("Тяга горизонтального блока") == "горизонтальная тяга"


def test_weekly_summary_and_overall(db):
    ingest_post(db, 2, at(25), "Жим штанги лежа – 4x8 60кг")
    ingest_post(db, 3, at(26), "Приседания со штангой – 4x8 80кг")
    ingest_post(db, 4, at(27), "70,6кг")
    text = stats.weekly_summary(db, date(2026, 10, 28))
    assert "Тренировок: 2 из 2" in text and "Тоннаж" in text and "70,6" in text
    assert "Тренировок: 2" in stats.overall(db, date(2026, 10, 28))
    assert stats.sparkline([1, 2, 3]) == "▁▅█"
