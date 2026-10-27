"""Database methods and the live flow: template -> logs -> next day -> edits."""
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from diary import program
from diary.db import Db
from diary.ingest import forget_post, ingest_post
from diary.today import build_plan, render_html, render_text

TEMPLATE = """НЕДЕЛЯ 1: ВВОД · RIR 3
ПОНЕДЕЛЬНИК: ВЕРХ А
Жим штанги лежа – 4x6-8
Тяга штанги в наклоне – 4x8-10

ВТОРНИК: НИЗ А
Приседания со штангой – 4x6-8
Планка – 3x60с

НЕДЕЛЯ 2: НАКОПЛЕНИЕ · RIR 2
ПОНЕДЕЛЬНИК: ВЕРХ А
Жим штанги лежа – 5x6-8
Тяга штанги в наклоне – 4x8-10

ВТОРНИК: НИЗ А
Приседания со штангой – 5x6-8
Планка – 3x60с
"""
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    return Db(tmp_path / "d.db")


def at(days):
    return T0 + timedelta(days=days)


def test_template_then_logs_advance_the_cycle(db):
    r = ingest_post(db, 1, at(0), TEMPLATE)
    assert r.kind == "template" and r.program_id
    program.activate(db, r.program_id)
    assert program.next_day(db)["title"] == "ПОНЕДЕЛЬНИК: ВЕРХ А"

    log = ingest_post(db, 2, at(4), "НЕДЕЛЯ 1: ВВОД · RIR 3\nПОНЕДЕЛЬНИК: ВЕРХ А\nЖим штанги лежа – 4x8 60кг\n"
                                     "Тяга штанги в наклоне – 4x10 60кг")
    assert (log.kind, log.sets) == ("log", 8)
    assert program.next_day(db)["title"] == "ВТОРНИК: НИЗ А"

    # a log without headers still matches by exercise overlap
    ingest_post(db, 3, at(5), "Приседания со штангой – 4x8 80кг\nПланка 3хмакс(70с; 60с; 67с)")
    nxt = program.next_day(db)
    assert (nxt["week"], nxt["title"]) == (2, "ПОНЕДЕЛЬНИК: ВЕРХ А")

    plan = build_plan(db, date(2026, 10, 12))
    by_name = {ln.name: ln for ln in plan.lines}
    assert by_name["Жим штанги лежа"].rec.weight is not None
    assert "Жим штанги лежа – 5x6-8" in render_text(plan)
    assert render_html(plan).startswith("<pre>")


def test_edit_replaces_sets_and_forget_removes(db):
    ingest_post(db, 10, at(0), "Жим лежа 3х8 60кг")
    assert db.counts()["sets"] == 3
    ingest_post(db, 10, at(0), "Жим лежа 4х8 60кг", edited=at(1))
    assert db.counts()["sets"] == 4
    assert db.post(10)["edited_utc"]
    assert forget_post(db, 10) and db.counts()["sets"] == 0


def test_skip_and_set_next(db):
    pid = ingest_post(db, 1, at(0), TEMPLATE).program_id
    program.activate(db, pid)
    assert program.skip(db)["ord"] == 1
    program.set_next(db, 3)
    assert program.next_day(db)["ord"] == 3
    # a later log wins over the manual position
    ingest_post(db, 2, datetime.now(timezone.utc) + timedelta(minutes=1),
                "НЕДЕЛЯ 1\nПОНЕДЕЛЬНИК: ВЕРХ А\nЖим штанги лежа – 4x8 60кг")
    assert program.next_day(db)["ord"] == 1


def test_edited_active_template_stays_active(db):
    pid = ingest_post(db, 1, at(0), TEMPLATE).program_id
    program.activate(db, pid)
    ingest_post(db, 1, at(0), TEMPLATE.replace("4x6-8", "4x5-7"), edited=at(1))
    assert db.active_program()["post_id"] == 1


def test_aliases_suggestions_and_merge(db):
    ingest_post(db, 1, at(0), "Разнибания ног сидя – 3x12 50кг")  # typo: same exercise
    ingest_post(db, 2, at(1), "Сгибания ног сидя – 3x12 27кг")    # a different exercise
    sugg = {s["name"]: s for s in db.open_suggestions()}
    assert "Сгибания ног сидя" in sugg
    s = sugg["Сгибания ног сидя"]
    db.set_suggestion(s["id"], "kept")
    assert not any(x["id"] == s["id"] for x in db.open_suggestions())

    a = db.exercise_by_alias("тяга к лицу")
    b = db.add_exercise("Лицевая тяга с канатом", "cable")
    db.merge_exercises(b, a)
    assert db.exercise(b) is None


def test_settings_body_and_sent(db):
    assert db.get("missing", "x") == "x"
    db.set("reminder_time", "07:30")
    assert db.get("reminder_time") == "07:30"
    assert db.bodyweight() is None
    ingest_post(db, 5, at(0), "70,6кг")
    assert db.bodyweight() == 70.6
    db.mark_sent(99, "2026-10-27")
    assert db.is_sent(99) and not db.is_sent(98)
    assert db.post_counts() == {"body": 1}
    assert not db.has_session_on("2026-10-01")


def test_variants_are_not_merged_silently(db):
    a = ingest_post(db, 1, at(0), "Подъём на носки сидя в тренажёре – 3x12 40кг")
    b = ingest_post(db, 2, at(1), "Подъём на носки стоя в тренажёре – 3x12 60кг")
    assert b.new_exercises and b.suggestions  # asked, not merged
    ingest_post(db, 3, at(2), "Отжимания на брусьях (акцент на грудь) 3x10")
    r = ingest_post(db, 4, at(3), "Отжимания на брусьях (акцент на трицепс) 3x10")
    assert r.new_exercises == ["Отжимания на брусьях (акцент на трицепс)"]
    assert ingest_post(db, 5, at(4), "Разнибания ног сидя – 3x12 50кг").new_exercises == []  # a typo merges
    assert a.kind == "log"


def test_dumbbell_aliases_of_a_barbell_lift_split_off(tmp_path):
    from diary import db as dbmod
    d = Db(tmp_path / "x.db")
    bar = d.exercise_by_alias("жим штанги на наклонной")
    d.add_alias("жим гантелей на наклонной 30", bar)
    d.add_alias("жим гантелей на наклонной", bar)
    d.conn.commit()
    d.conn.execute("PRAGMA user_version = 0")
    d.conn.close()
    d = Db(tmp_path / "x.db")
    dumb = d.exercise_by_alias("жим гантелей на наклонной")
    assert dumb != bar and d.exercise_by_alias("жим гантелей на наклонной 30") == dumb
    assert d.exercise(dumb)["equipment"] == "dumbbell"
    assert d.one("PRAGMA user_version")[0] == len(dbmod.MIGRATIONS)


def test_failed_migration_leaves_nothing_half_done(tmp_path, monkeypatch):
    from diary import db as dbmod
    d = Db(tmp_path / "m.db")
    d.conn.close()
    monkeypatch.setattr(dbmod, "MIGRATIONS", [*dbmod.MIGRATIONS, "ALTER TABLE post ADD COLUMN foo TEXT;\nSELECT * FROM nope;"])
    with pytest.raises(Exception):
        Db(tmp_path / "m.db")
    conn = sqlite3.connect(tmp_path / "m.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(post)")]
    assert "foo" not in cols and conn.execute("PRAGMA user_version").fetchone()[0] == len(dbmod.MIGRATIONS) - 1


def test_editing_away_a_typo_drops_the_typo_exercise(db):
    ingest_post(db, 1, at(0), "Жим лежа 3x8 60кг")
    r = ingest_post(db, 2, at(1), "Жим лажа узким 3x8 50кг")
    assert r.new_exercises
    n = db.exercise_count()
    ingest_post(db, 2, at(1), "Жим лежа 3x8 50кг", edited=at(2))
    assert db.exercise_count() == n - 1 and not db.open_suggestions()


def test_bare_weekday_note_and_body_weight_in_a_workout(db):
    pid = ingest_post(db, 1, at(0), TEMPLATE).program_id
    program.activate(db, pid)
    assert ingest_post(db, 2, at(1), "Пн не приду, работа").kind == "note"
    assert program.next_day(db)["ord"] == 0  # the note did not take Monday
    ingest_post(db, 3, at(2), "Вес 70,8\nЖим штанги лежа – 4x8 60кг")
    assert db.bodyweight() == 70.8
    ingest_post(db, 4, at(3), "вчера\n70,4кг")
    assert db.body_series()[-1]["day"] == (at(3) - timedelta(days=1)).date().isoformat()


def test_late_post_does_not_become_the_last_workout(db):
    ingest_post(db, 1, at(10), "Жим лежа 4x8 60кг - не добавлять")
    ingest_post(db, 2, at(11), "08.10\nЖим лежа 4x8 75кг")  # a forgotten older workout posted later
    eid = db.exercise_by_alias("жим лежа")
    from diary.today import history
    assert history(db, eid)[0].top == 60


def test_exercises_beat_a_wrong_weekday(db):
    pid = ingest_post(db, 1, at(0), TEMPLATE).program_id
    program.activate(db, pid)
    ingest_post(db, 2, at(1), "ВТОРНИК\nЖим штанги лежа – 4x8 60кг\nТяга штанги в наклоне – 4x10 60кг")
    assert program.next_day(db)["title"] == "ВТОРНИК: НИЗ А"  # matched Monday by the exercises
