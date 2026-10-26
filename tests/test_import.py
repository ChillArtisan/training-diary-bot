"""Acceptance: the bundled demo export always, a real export when DIARY_EXPORT points to one."""
import os
from datetime import date
from pathlib import Path

import pytest

from diary import program, stats
from diary.db import Db
from diary.ingest import import_export
from diary.today import build_plan

DEMO = Path(__file__).parent.parent / "examples" / "demo_export"
REAL = Path(os.environ["DIARY_EXPORT"]) if os.environ.get("DIARY_EXPORT") else None


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    db = Db(tmp_path_factory.mktemp("demo") / "d.db")
    return db, import_export(db, DEMO)


def test_demo_imports_cleanly(demo):
    db, r = demo
    assert r.kinds == {"template": 1, "log": 6, "body": 2}
    assert r.parse_share == 1.0 and not db.open_suggestions()


def test_demo_reimport_changes_nothing(demo):
    db, _ = demo
    before = (db.counts(), db.post_counts())
    import_export(db, DEMO)
    assert (db.counts(), db.post_counts()) == before


def test_demo_plan(demo):
    db, _ = demo
    assert program.next_day(db)["title"] == "ЧЕТВЕРГ: НИЗ"
    plan = build_plan(db, date(2026, 10, 1))
    by_name = {ln.name: ln.rec for ln in plan.lines}
    assert "не добавлять" in by_name["Румынская тяга"].reason and by_name["Румынская тяга"].weight == 65
    assert by_name["Планка"].seconds == 70
    assert db.one("SELECT day FROM session WHERE post_id = 7")["day"] == "2026-09-24"  # «вчера» posted on the 25th


def test_demo_stats(demo):
    db, _ = demo
    assert stats.records_between(db, date(2026, 9, 1), date(2026, 9, 30))
    assert "обхват груди: 101" in stats.measurements_text(db)


@pytest.mark.skipif(REAL is None, reason="set DIARY_EXPORT to a real channel export")
def test_real_export(tmp_path):
    db = Db(tmp_path / "d.db")
    r = import_export(db, REAL)
    assert r.parse_share >= 0.95
    before = db.counts()
    import_export(db, REAL)
    assert db.counts() == before
    assert all(ln.rec.reason for ln in build_plan(db, date.today()).lines)
