"""Goals: parsing «/goal …», the robust trend, the forecast text, achievements and the /goal command."""
from datetime import date, datetime, timedelta, timezone

import pytest

from diary import goals
from diary.db import Db
from diary.ingest import ingest_post

TODAY = date(2026, 10, 27)
T0 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "g.db")
    w = 60.0
    for i in range(16):  # a slow, steady bench: +1,25 kg every two weeks
        ingest_post(d, i + 1, T0 + timedelta(days=7 * i), f"Жим лежа 3x5 {w:g}кг")
        w += 1.25 if i % 2 else 0
    return d


def bench(db):
    return db.exercise_by_alias("жим лежа")


def test_parse_goal():
    g = goals.parse_goal("жим лёжа 100", TODAY)
    assert (g.kind, g.name, g.target, g.reps, g.deadline) == ("lift", "жим лёжа", 100, None, None)
    g = goals.parse_goal("присед 90x5 к 01.03", TODAY)
    assert (g.target, g.weight, g.reps, g.deadline) == (105, 90, 5, date(2027, 3, 1))
    g = goals.parse_goal("вес 75 к лету", TODAY)
    assert (g.kind, g.target, g.deadline) == ("body", 75, date(2027, 6, 1))
    assert goals.parse_goal("тяга 140 через 3 месяца", TODAY).deadline == TODAY + timedelta(days=90)
    assert goals.parse_goal("просто текст", TODAY) is None
    assert goals.parse_goal("жим 100 к когда-нибудь", TODAY) is None


def test_trend_ignores_one_outlier():
    pts = [(TODAY - timedelta(days=7 * (8 - i)), 80 + i) for i in range(8)]
    pts[4] = (pts[4][0], 120)  # one wild day
    level, slope, *_ = goals.trend(pts, TODAY)
    assert 0.12 < slope < 0.16 and 87 < level < 90  # about +1 kg a week, as the other days say


def test_too_little_data_means_no_forecast():
    pts = [(TODAY - timedelta(days=d), 80) for d in (10, 5, 1)]
    assert goals.trend(pts, TODAY) is None


def test_forecast_and_deadlines(db):
    g = goals.set_goal(db, TODAY, "lift", 90, bench(db))
    f = goals.forecast(db, g, TODAY)
    assert f.status == "eta" and f.eta and 0.5 < f.pace < 1
    assert "темп +0,7 кг/нед" in goals.describe(f, TODAY)
    late = goals.forecast(db, goals.set_goal(db, TODAY, "lift", 90, bench(db), deadline=date(2027, 1, 1)), TODAY)
    assert late.status == "late" and "нужно" in goals.describe(late, TODAY)
    ok = goals.forecast(db, goals.set_goal(db, TODAY, "lift", 90, bench(db), deadline=date(2027, 12, 1)), TODAY)
    assert ok.status == "on_track" and "успеваешь" in goals.describe(ok, TODAY)
    assert len(goals.all_goals(db)) == 1  # one goal per exercise


def test_reached_goal_is_marked_once(db):
    goals.set_goal(db, TODAY, "lift", 80, bench(db))
    assert [g.target for g in goals.check_achieved(db, TODAY)] == [80]
    assert goals.check_achieved(db, TODAY) == []
    assert goals.all_goals(db) == [] and len(goals.all_goals(db, with_done=True)) == 1


def test_body_goal_goes_down_too(tmp_path):
    d = Db(tmp_path / "b.db")
    for i, w in enumerate([80, 79.6, 79.1, 78.9, 78.2, 78.0]):
        ingest_post(d, i + 1, T0 + timedelta(days=7 * i), f"{w:g}кг".replace(".", ","))
    g = goals.set_goal(d, date(2026, 8, 12), "body", 75)
    f = goals.forecast(d, g, date(2026, 8, 12))
    assert f.status == "eta" and f.pace < 0 and "−0," in goals.describe(f, date(2026, 8, 12))


def test_projection_and_dropping(db):
    g = goals.set_goal(db, TODAY, "lift", 90, bench(db))
    line = goals.projection(goals.forecast(db, g, TODAY), TODAY)
    assert line[0][0] == TODAY and line[1][1] > line[0][1]
    assert goals.drop_goal(db, g.id) and goals.all_goals(db) == []


def test_goal_command(tmp_path, monkeypatch):
    from tests.test_bot_handlers import build, dm_message, feed, make_ident, texts
    d, ident = make_ident(tmp_path, monkeypatch, owner=7)
    ingest_post(d, 1, T0, "Жим лежа 3x5 60кг")
    dp, bot, session = build(d, ident)
    feed(dp, bot, message=dm_message("/goal"))
    assert "/goal жим лёжа 100" in texts(session)[-1]
    feed(dp, bot, message=dm_message("/goal жим лежа 100 к лету"))
    assert any("Жим штанги лёжа: 100 кг на раз к 01.06" in t for t in texts(session))
    feed(dp, bot, message=dm_message("/goal кульбит назад 200"))
    assert "Не нашёл" in texts(session)[-1]
