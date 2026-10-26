"""Analytics numbers and chart rendering."""
from datetime import date, datetime, timedelta, timezone

import pytest

from diary import analytics, charts, program
from diary.db import Db
from diary.ingest import ingest_post
from tests.test_flow import TEMPLATE

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)  # Thursday
PNG = b"\x89PNG"


def at(days):
    return T0 + timedelta(days=days)


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "d.db")
    program.activate(d, ingest_post(d, 1, at(0), TEMPLATE).program_id)
    return d


def week1(d, mid, day, bench=60, row=60, reps=8):
    ingest_post(d, mid, at(day), f"НЕДЕЛЯ 1\nПОНЕДЕЛЬНИК: ВЕРХ А\nЖим штанги лежа – 4x{reps} {bench}кг\n"
                                 f"Тяга штанги в наклоне – 4x10 {row}кг")


def test_kpd_full_session(db):
    week1(db, 2, 4)
    k = analytics.kpd_series(db)[-1]
    assert (k.planned, k.done, k.hit, k.graded) == (2, 2, 8, 8)
    assert k.score == 100 and "2/2 упражнений" in k.text()


def test_kpd_partial_and_missed_reps(db):
    ingest_post(db, 2, at(4), "НЕДЕЛЯ 1\nПОНЕДЕЛЬНИК: ВЕРХ А\nЖим штанги лежа – 4x6 60кг (1-6; 2-6; 3-5; 4-4)")
    k = analytics.kpd_series(db)[-1]
    assert (k.planned, k.done) == (2, 1)
    assert (k.hit, k.graded) == (2, 4)  # 4x6-8: two sets reached 6
    assert k.score == pytest.approx(100 * (0.5 * 0.4 + 0.5 * 0.4) / 0.8)


def test_kpd_progress_compares_the_same_program_day(db):
    week1(db, 2, 4, bench=60)
    ingest_post(db, 3, at(5), "ВТОРНИК: НИЗ А\nПриседания со штангой – 4x8 80кг")
    ingest_post(db, 4, at(11), "НЕДЕЛЯ 2\nПОНЕДЕЛЬНИК: ВЕРХ А\nЖим штанги лежа – 5x6 70кг\nТяга штанги в наклоне – 4x10 60кг")
    ingest_post(db, 5, at(12), "ВТОРНИК: НИЗ А\nПриседания со штангой – 5x6 85кг")
    week1(db, 6, 18, bench=62.5, row=55)  # next cycle: bench up, row down vs week 1, not vs week 2
    k = analytics.kpd_series(db)[-1]
    assert (k.up, k.same, k.down) == (1, 0, 1)


def test_post_kpd(db):
    week1(db, 2, 4)
    assert analytics.post_kpd(db, 2).score == 100
    assert analytics.post_kpd(db, 999) is None


def test_consistency_and_return_after_break(db):
    for i, day in enumerate([4, 5, 11, 12]):  # two full weeks in October 2026
        ingest_post(db, 10 + i, at(day), "Жим штанги лежа – 4x8 60кг")
    for i, day in enumerate([60, 61, 67, 68]):  # a break, then two more weeks
        ingest_post(db, 20 + i, at(day), "Жим штанги лежа – 4x8 60кг")
    today = (T0 + timedelta(days=70)).date()
    c = analytics.consistency(db, today)
    assert c.planned == 2
    assert c.since_return and c.since_return[1] == 1.0
    assert c.best_streak >= 2 and c.longest_break > 40
    assert set(c.weekday_share) == {0, 1}
    text = analytics.report_text(db, today)
    assert "Постоянство" in text and "С возвращения" in text


def test_volume_by_muscle(db):
    week1(db, 2, 4)
    vol = analytics.weekly_volume(db, date(2026, 10, 10), by="muscle")
    assert vol == {"грудь": 1.0, "спина": 1.0}


def test_main_lifts_and_strength_points(db):
    for i, day in enumerate([4, 11, 18]):
        week1(db, 2 + i, day, bench=60 + 2.5 * i)
    lifts = analytics.main_lifts(db, date(2026, 10, 25))
    assert lifts[0][1] in ("Жим штанги лёжа", "Тяга штанги в наклоне")
    bench = db.exercise_by_alias("жим штанги лежа")
    pts = analytics.strength_points(db, bench)
    assert [p[2] for p in pts] == [60, 62.5, 65]


def test_charts_render(db):
    for i, day in enumerate([4, 11, 18]):
        week1(db, 2 + i, day, bench=60 + 2.5 * i)
    ingest_post(db, 9, at(5), "70,6кг")
    ingest_post(db, 10, at(15), "70,1кг")
    today = date(2026, 10, 25)
    bench = db.exercise_by_alias("жим штанги лежа")
    c = analytics.consistency(db, today, 26)
    pngs = [
        charts.strength("Жим", analytics.strength_points(db, bench)),
        charts.consistency(c.training_days, analytics.planned_weekdays(db), today, 26, "", date(2026, 10, 1)),
        charts.weekly(analytics.weekly_series(db, today), c.planned),
        charts.kpd(analytics.kpd_points(db)),
        charts.volume(analytics.weekly_volume(db, today, by="muscle")),
        charts.bodyweight(analytics.bodyweight_points(db)),
    ]
    assert all(p and p.startswith(PNG) for p in pngs)


def test_charts_need_data():
    assert charts.strength("x", [(date(2026, 1, 1), 100, 80)]) is None
    assert charts.kpd([]) is None and charts.volume({}) is None and charts.bodyweight([]) is None


def test_progress_menu(db):
    from bot import ui
    for i, day in enumerate([4, 11, 18]):
        week1(db, 2 + i, day)
    menu = ui.progress_menu(db, date(2026, 10, 25))
    data = [b.callback_data for row in menu.inline_keyboard for b in row]
    assert "chart:body" in data and "chart:kpd" in data and any(d.split(":")[1].isdigit() for d in data)
    assert ui.BTN_ANALYTICS in {b.text for row in ui.MAIN.keyboard for b in row}


def test_overall_is_labelled(db):
    from diary import stats
    for i, day in enumerate([4, 11, 18]):
        week1(db, 2 + i, day, bench=60 + 2.5 * i)
        ingest_post(db, 20 + i, at(day + 1), f"Подтягивания с весом – 4x8 +{5 + i}кг")
    ingest_post(db, 30, at(2), "70,6кг")
    text = stats.overall(db, date(2026, 12, 1))
    assert "Максимум на 1 повтор" in text and "≈" in text and "65кг×8" in text
    assert "Со своим весом" in text and "Подтягивания с весом" in text.split("Со своим весом")[1]
    assert "на 03.10.26" in text and "дн. назад" in text
    assert "Тоннаж</b> — сумма «вес × повторы»" in text
