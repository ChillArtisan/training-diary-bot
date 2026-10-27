"""Regression tests for the analytics review fixes: KPD, pauses, volume span, movement groups, charts."""
from datetime import date, datetime, timedelta, timezone

import pytest

from diary import analytics, charts, clock, movement, program, state, stats
from diary.db import Db
from diary.ingest import ingest_post
from tests.test_flow import TEMPLATE

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)  # Thursday


def at(days):
    return T0 + timedelta(days=days)


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "d.db")
    program.activate(d, ingest_post(d, 1, at(0), TEMPLATE).program_id)
    return d


def monday(d, mid, day, bench="60кг", row="60кг", reps=8, title="НЕДЕЛЯ 1"):
    ingest_post(d, mid, at(day), f"{title}\nПОНЕДЕЛЬНИК: ВЕРХ А\nЖим штанги лежа – 4x{reps} {bench}\n"
                                 f"Тяга штанги в наклоне – 4x10 {row}")


def last_kpd(d):
    return analytics.kpd_series(d)[-1]


# ---------- 1. progress at any rep count ----------

def test_progress_counts_sets_over_twelve_reps(db):
    ingest_post(db, 2, at(4), "Разгибания у блока с канатом – 3x15 20кг")
    ingest_post(db, 3, at(11), "Разгибания у блока с канатом – 3x20 25кг")
    k = last_kpd(db)
    assert (k.up, k.same, k.down) == (1, 0, 0)


@pytest.mark.parametrize("first, second, verdict", [
    ("3x10 60кг", "3x9 62,5кг", (1, 0, 0)),     # heavier, one rep fewer
    ("3x10 60кг", "3x6 62,5кг", (0, 0, 1)),     # heavier but the reps collapsed
    ("3x10 60кг", "3x11 60кг", (1, 0, 0)),      # same weight, more reps
    ("3x10 60кг", "3x10 60кг", (0, 1, 0)),
    ("3x10 60кг", "3x9 60кг", (0, 0, 1)),
    ("3x15 20кг", "3x14 22,5кг", (1, 0, 0)),    # over 12 reps both
    ("3x15 20кг", "3x20 19кг", (1, 0, 0)),    # lighter but e1RM up
])
def test_progress_rule(db, first, second, verdict):
    ingest_post(db, 2, at(4), f"Жим гантелей сидя – {first}")
    ingest_post(db, 3, at(11), f"Жим гантелей сидя – {second}")
    k = last_kpd(db)
    assert (k.up, k.same, k.down) == verdict


def test_progress_timed_sets(db):
    ingest_post(db, 2, at(4), "Планка 3хмакс(60с; 55с; 50с)")
    ingest_post(db, 3, at(11), "Планка 3хмакс(70с; 55с; 50с)")
    assert last_kpd(db).up == 1


# ---------- 2. completion against the remembered plan ----------

def _note(db, day, mode, items):
    d = db.program_days(db.active_program()["id"])[0]
    db.set_json(f"plan:{day}", {"day_id": d["id"], "mode": mode, "items": items})


def test_short_mode_is_complete(db):
    ingest_post(db, 2, at(4), "НЕДЕЛЯ 1\nПОНЕДЕЛЬНИК: ВЕРХ А\nЖим штанги лежа – 3x8 60кг")
    assert last_kpd(db).completion == 0.5
    bench = db.exercise_by_alias("жим штанги лежа")
    _note(db, "2026-10-05", "short", [[bench, 3]])
    k = last_kpd(db)
    assert k.completion == 1.0 and (k.planned, k.done) == (1, 1)


def test_swapped_exercise_counts_as_planned(db):
    ingest_post(db, 2, at(4), "НЕДЕЛЯ 1\nПОНЕДЕЛЬНИК: ВЕРХ А\nЖим гантелей лежа – 4x8 24кг\n"
                              "Тяга штанги в наклоне – 4x10 60кг")
    assert last_kpd(db).completion == 0.5
    dumbbell = db.exercise_by_alias("жим гантелей лежа")
    row = db.exercise_by_alias("тяга штанги в наклоне")
    _note(db, "2026-10-05", "normal", [[dumbbell, 4], [row, 4]])
    assert last_kpd(db).completion == 1.0


def test_fewer_sets_than_planned_lowers_completion(db):
    ingest_post(db, 2, at(4), "НЕДЕЛЯ 1\nПОНЕДЕЛЬНИК: ВЕРХ А\nЖим штанги лежа – 2x8 60кг")
    bench = db.exercise_by_alias("жим штанги лежа")
    _note(db, "2026-10-05", "normal", [[bench, 4]])
    assert last_kpd(db).completion == 0.5


def test_light_day_has_no_progress(db):
    monday(db, 2, 4)
    monday(db, 3, 11, bench="70кг")
    assert last_kpd(db).progress is not None
    bench = db.exercise_by_alias("жим штанги лежа")
    row = db.exercise_by_alias("тяга штанги в наклоне")
    _note(db, "2026-10-12", "light", [[bench, 4], [row, 4]])
    k = last_kpd(db)
    assert k.light and k.progress is None and "лёгкий день" in k.text()


def test_deload_by_phase_name_only(db):
    monday(db, 2, 4)
    monday(db, 3, 11, bench="40кг", row="40кг", title="НЕДЕЛЯ 3: РАЗГРУЗКА")
    k = last_kpd(db)
    assert k.deload and k.progress is None


# ---------- 3. pauses ----------

def test_pause_log_survives_resume(db):
    state.pause(db, date(2026, 10, 12), None)
    assert state.is_paused(db, date(2026, 10, 13))
    state.resume(db, date(2026, 10, 25))
    assert not state.is_paused(db, date(2026, 10, 26))
    assert state.paused_days(db, date(2026, 11, 1)) == [(date(2026, 10, 12), date(2026, 10, 25))]
    assert state.paused_weeks(db, date(2026, 11, 1)) == {date(2026, 10, 12), date(2026, 10, 19)}


def test_resume_with_one_argument_and_open_interval(db):
    today = clock.today()
    state.pause(db, today, None)
    assert state.paused_days(db) == [(today, today)]  # the open pause is cut at today
    state.resume(db)
    assert state.paused_days(db) == [(today, today)] and state.paused_until(db) is None


def test_paused_weeks_drop_out_of_consistency_and_streak(db):
    for i, day in enumerate([4, 5, 25, 26]):  # Oct 5-6 and Oct 26-27, nothing in between
        ingest_post(db, 10 + i, at(day), "Жим штанги лежа – 4x8 60кг")
    today = date(2026, 11, 2)
    before = analytics.consistency(db, today, 4)
    assert before.adherence == 0.5 and before.best_streak == 1
    state.pause(db, date(2026, 10, 12), None)
    state.resume(db, date(2026, 10, 25))
    c = analytics.consistency(db, today, 4)
    assert c.adherence == 1.0 and c.paused == {date(2026, 10, 12), date(2026, 10, 19)}
    assert c.best_streak == 2
    assert stats.streak(db, today) == 2


def test_short_pause_is_not_a_paused_week(db):
    state.pause(db, date(2026, 10, 12), date(2026, 10, 14))  # 3 days only
    assert state.paused_weeks(db, date(2026, 11, 1)) == set()


def test_consistency_chart_accepts_paused():
    png = charts.consistency({date(2026, 10, 5)}, {0, 1}, date(2026, 10, 25), 4, "", date(2026, 10, 1),
                             paused={date(2026, 10, 12)})
    assert png.startswith(b"\x89PNG")


# ---------- 4. volume over the real span ----------

def test_volume_divides_by_weeks_of_data(db):
    monday(db, 2, 4)
    monday(db, 3, 11)
    vol = analytics.weekly_volume(db, date(2026, 10, 18), by="muscle")
    assert vol["грудь"] == 4.0 and vol["спина"] == 4.0  # 8 sets over 2 weeks, not over 4


def test_volume_needs_two_weeks_for_the_low_insight(db):
    monday(db, 2, 4)
    assert "маловато" not in analytics.report_text(db, date(2026, 10, 10))
    monday(db, 3, 11)
    assert "маловато" in analytics.report_text(db, date(2026, 10, 18))


def test_volume_full_window(db):
    for i, day in enumerate([4, 11, 18, 25]):
        monday(db, 2 + i, day)
    assert analytics.weekly_volume(db, date(2026, 11, 1), by="muscle")["грудь"] == 4.0


# ---------- 5. movement groups ----------

@pytest.mark.parametrize("name, group", [
    ("Французский жим лёжа", "трицепс"),
    ("Французский жим лежа", "трицепс"),
    ("Французский жим сидя", "трицепс"),
    ("Французский жим", "трицепс"),
    ("Разгибания рук с канатом", "трицепс"),
    ("Разгибания на трицепс в блоке", "трицепс"),
    ("Отжимания обратные", "трицепс"),
    ("Жим в тренажере сидя вверх", "жим вверх"),
    ("Жим гантелей сидя", "жим вверх"),
    ("Армейский жим стоя", "жим вверх"),
    ("Жим штанги лёжа", "жим лёжа"),
    ("Жим гантелей лёжа", "жим лёжа"),
    ("Жим в тренажере", "жим лёжа"),
    ("Отжимания", "жим лёжа"),
    ("Жим штанги на наклонной", "жим на наклонной"),
    ("Жим ногами", "жим ногами"),
    ("Тяга с пола", "тяга с пола"),
    ("Румынская тяга", "тяга с пола"),
    ("Вертикальная тяга", "вертикальная тяга"),
    ("Горизонтальная тяга", "горизонтальная тяга"),
    ("Тяга штанги в наклоне", "горизонтальная тяга"),
    ("Лицевая тяга с канатом", "задняя дельта"),
    ("Махи гантелями в стороны", "средняя дельта"),
    ("Приседания со штангой", "присед"),
    ("Разгибания ног сидя", "разгибание ног"),
    ("Сгибания ног лёжа", "сгибание ног"),
    ("Планка", "пресс"),
    ("Подъём штанги на бицепс", "бицепс"),
])
def test_movement_groups(name, group):
    assert movement.group_of(name) == group


# ---------- 7. degenerate chart input ----------

NAN = float("nan")
D = date(2026, 10, 5)


@pytest.mark.parametrize("make", [
    lambda: charts.kpd([]),
    lambda: charts.kpd([(D, 80)]),
    lambda: charts.kpd([(D, NAN), (D, NAN)]),
    lambda: charts.kpd([(D, NAN), (D, 70), (D + timedelta(days=1), 90)]),
    lambda: charts.volume({}),
    lambda: charts.volume({"грудь": NAN}),
    lambda: charts.volume({"грудь": 0.0}),
    lambda: charts.volume({"грудь": 12, "спина": NAN}),
    lambda: charts.bodyweight([]),
    lambda: charts.bodyweight([(D, 70)]),
    lambda: charts.bodyweight([(D, NAN), (D, NAN)]),
    lambda: charts.weekly([], 3),
    lambda: charts.weekly([(D, 0, 0.0)], 3),
    lambda: charts.weekly([(D, 2, NAN)], 3),
    lambda: charts.weekly([(D, 0, 0.0), (D + timedelta(days=7), 0, NAN)], 0),
    lambda: charts.consistency(set(), set(), D, 1),
    lambda: charts.consistency(set(), {0}, D, 4, "", None, set()),
    lambda: charts.strength("x", []),
    lambda: charts.strength("x", [(D, 100, None)]),
])
def test_charts_survive_degenerate_data(make):
    out = make()
    assert out is None or out.startswith(b"\x89PNG")


def test_charts_drop_nan_but_keep_the_rest():
    pts = [(D, NAN), (D, 70), (D + timedelta(days=1), 90)]
    assert charts.kpd(pts).startswith(b"\x89PNG")
    assert charts.weekly([(D, 2, NAN)], 3).startswith(b"\x89PNG")
