"""Bot wiring and scheduled jobs with a fake Telegram (no network)."""
import asyncio
import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from diary import program, state
from diary.db import Db
from diary.ingest import MSK, ingest_post
from tests.test_flow import TEMPLATE


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.docs: list[int] = []
        self.photos: list[tuple[int, str]] = []
        self._ids = itertools.count(1000)

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=next(self._ids), date=datetime.now(timezone.utc))

    async def send_document(self, chat_id, document, **kw):
        self.docs.append(chat_id)

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.photos.append((chat_id, caption or ""))
        return SimpleNamespace(message_id=next(self._ids))

    async def send_media_group(self, chat_id, media, **kw):
        self.photos += [(chat_id, "") for _ in media]
        return []


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("OWNER_ID", "7")
    monkeypatch.setenv("CHANNEL_ID", "-1001234567890")
    from bot.common import Alerts, Identity
    from bot.config import Config
    db = Db(tmp_path / "d.db")
    pid = ingest_post(db, 1, datetime(2026, 10, 1, tzinfo=timezone.utc), TEMPLATE).program_id
    program.activate(db, pid)
    return db, Identity(db, Config.from_env()), Alerts(), FakeBot()


def run(coro):
    return asyncio.run(coro)


def test_routers_build(env):
    db, ident, _, _ = env
    from bot.channel import build_channel_router
    from bot.owner import build_claim_router, build_owner_router
    names = [r.name for r in (build_channel_router(db, ident), build_claim_router(db, ident),
                              build_owner_router(db, ident))]
    assert names == ["channel", "claim", "owner"]
    assert ident.post_link(5) == "https://t.me/c/1234567890/5"


def test_identity_learned_when_env_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.delenv("OWNER_ID", raising=False)
    monkeypatch.delenv("CHANNEL_ID", raising=False)
    from bot.common import Identity
    from bot.config import Config
    ident = Identity(Db(tmp_path / "d.db"), Config.from_env())
    assert ident.owner_id is None and ident.channel_id is None
    ident.set_channel(-100555)
    ident.set_owner(42)
    assert (ident.owner_id, ident.channel_id) == (42, -100555)


def test_morning_posts_plan_once(env):
    from bot.scheduler import tick
    db, ident, alerts, bot = env
    monday = datetime(2026, 10, 26, 8, 5, tzinfo=MSK)
    assert "morning" in run(tick(bot, db, ident, alerts, monday))
    assert bot.sent[-1][0] == ident.channel_id and "<pre>" in bot.sent[-1][1]
    assert "morning" not in run(tick(bot, db, ident, alerts, monday + timedelta(minutes=5)))


def test_morning_respects_time_day_pause_and_logs(env):
    from bot.scheduler import JOBS, due
    db, *_ = env
    morning = next(j for j in JOBS if j.name == "morning")
    monday = datetime(2026, 10, 26, 8, 5, tzinfo=MSK)
    assert due(morning, db, monday)
    assert not due(morning, db, monday.replace(hour=7))
    assert not due(morning, db, monday.replace(hour=11))
    assert not due(morning, db, monday + timedelta(days=2))  # Wednesday is not in the program
    db.set("reminder", "off")
    assert not due(morning, db, monday)
    db.set("reminder", "on")
    state.pause(db, monday.date(), None)
    assert not due(morning, db, monday)
    state.resume(db)
    ingest_post(db, 2, datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc), "Жим штанги лежа – 4x8 60кг")
    assert not due(morning, db, monday)


def test_evening_nudge_and_weekly_summary(env):
    from bot.scheduler import tick
    db, ident, alerts, bot = env
    tuesday_evening = datetime(2026, 10, 27, 21, 40, tzinfo=MSK)
    assert "nudge" in run(tick(bot, db, ident, alerts, tuesday_evening))
    assert bot.sent[-1][0] == 7
    ingest_post(db, 2, datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc), "Жим штанги лежа – 4x8 60кг")
    sunday = datetime(2026, 11, 1, 20, 10, tzinfo=MSK)
    ran = run(tick(bot, db, ident, alerts, sunday))
    assert "summary" in ran and "monthly" in ran  # 1 November is a Sunday
    assert any("Тренировок" in caption for _, caption in bot.photos)  # the weekly chart carries the summary


def test_inactivity_once_per_gap(env):
    from bot.scheduler import tick
    db, ident, alerts, bot = env
    ingest_post(db, 2, datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc), "Жим штанги лежа – 4x8 60кг")
    noon = datetime(2026, 10, 20, 12, 30, tzinfo=MSK)
    assert "inactivity" in run(tick(bot, db, ident, alerts, noon))
    assert "inactivity" not in run(tick(bot, db, ident, alerts, noon + timedelta(days=1)))


def test_backup_daily_and_sent_on_monday(env):
    from bot.scheduler import tick
    db, ident, alerts, bot = env
    monday = datetime(2026, 10, 26, 4, 30, tzinfo=MSK)
    assert "backup" in run(tick(bot, db, ident, alerts, monday))
    assert bot.docs == [7]
    assert (db.path.parent / "backups" / "diary-2026-10-26.db").exists()


def test_failing_job_is_reported_not_raised(env, monkeypatch):
    import bot.scheduler as sch
    db, ident, alerts, bot = env

    async def boom(*a):
        raise RuntimeError("disk full")

    monkeypatch.setattr(sch.JOBS[-1], "run", boom)
    run(sch.tick(bot, db, ident, alerts, datetime(2026, 10, 27, 4, 30, tzinfo=MSK)))
    assert any("disk full" in text for _, text in bot.sent)


def test_chunks_split_long_text():
    from bot.common import chunks
    parts = chunks("\n".join("строка " * 20 for _ in range(200)), size=500)
    assert all(len(p) <= 500 for p in parts) and len(parts) > 10


def test_prefs_validation():
    from bot import prefs
    assert prefs.validate("reminder_time", "7:30") == "7:30"
    assert prefs.validate("reminder_time", "25:00") is None
    assert prefs.validate("dumbbell_steps", "10 12,5 15") == "10 12.5 15"
    assert prefs.validate("nope", "1") is None


def test_parse_until():
    from bot.owner import parse_until
    today = datetime(2026, 10, 27).date()
    assert parse_until("", today) is None
    assert parse_until("7", today) == today + timedelta(days=7)
    assert parse_until("15.11", today).isoformat() == "2026-11-15"
    assert parse_until("01.02", today).isoformat() == "2027-02-01"
    assert parse_until("завтра", today) == "bad"
