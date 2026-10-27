"""Owner/channel handlers through a real Dispatcher and a Bot whose HTTP session is a recorder."""
import asyncio
import itertools
import re
from datetime import datetime, timedelta, timezone

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetChatMember, SendMessage
from aiogram.types import (CallbackQuery, Chat, ChatMemberAdministrator, ChatMemberLeft, ChatMemberMember,
                           ChatMemberOwner, ChatMemberUpdated, InaccessibleMessage, Message, MessageOriginChannel,
                           Update, User)

from diary import backup
from diary.db import Db
from diary.ingest import MSK, ingest_post
from tests.test_bot_smoke import FakeBot, env, run  # noqa: F401  (env is a fixture)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
CHANNEL = -1001234567890
BOT_ID = 123
IDS = itertools.count(1)
LOG = "Жим штанги лежа – 4x8 60кг"


class Session(BaseSession):
    """Records every API call; admins is a set of (chat_id, user_id); fail(method) may return an exception."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.admins = set()
        self.fail = lambda method: None

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if (exc := self.fail(method)) is not None:
            raise exc
        if isinstance(method, SendMessage):
            return Message(message_id=next(IDS), date=NOW, chat=Chat(id=method.chat_id, type="private"),
                           text=method.text)
        if isinstance(method, GetChatMember):
            user = User(id=method.user_id, is_bot=False, first_name="x")
            if (method.chat_id, method.user_id) in self.admins:
                return ChatMemberOwner(user=user, is_anonymous=False)
            return ChatMemberMember(user=user)
        return True

    def of(self, kind):
        return [c for c in self.calls if isinstance(c, kind)]


def build(db, ident, alerts=None):
    from bot.channel import build_channel_router
    from bot.owner import build_claim_router, build_fallback_router, build_owner_router
    dp = Dispatcher()
    for r in (build_channel_router(db, ident), build_claim_router(db, ident),
              build_owner_router(db, ident, alerts), build_fallback_router()):
        dp.include_router(r)
    session = Session()
    return dp, Bot(f"{BOT_ID}:ABC", session=session), session


def user(uid=7):
    return User(id=uid, is_bot=False, first_name="u")


def dm_message(text=None, uid=7, **kw):
    return Message(message_id=next(IDS), date=NOW, chat=Chat(id=uid, type="private"), from_user=user(uid),
                   text=text, **kw)


def forwarded(chat_id=CHANNEL, mid=55, text=LOG, uid=7):
    origin = MessageOriginChannel(type="channel", date=NOW, chat=Chat(id=chat_id, type="channel", title="Дневник"),
                                  message_id=mid)
    return dm_message(text, uid, forward_origin=origin)


def callback(data, uid=7):
    gone = InaccessibleMessage(chat=Chat(id=uid, type="private"), message_id=next(IDS))
    return CallbackQuery(id="1", from_user=user(uid), chat_instance="c", data=data, message=gone)


def feed(dp, bot, **update):
    return asyncio.run(dp.feed_update(bot, Update(update_id=next(IDS), **update)))


def make_ident(tmp_path, monkeypatch, owner=None, channel=None):
    from bot.common import Identity
    from bot.config import Config
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    for name, value in (("OWNER_ID", owner), ("CHANNEL_ID", channel)):
        monkeypatch.delenv(name, raising=False)
        if value:
            monkeypatch.setenv(name, str(value))
    db = Db(tmp_path / "d.db")
    return db, Identity(db, Config.from_env())


def texts(session):
    return [c.text for c in session.of(SendMessage)]


# ---------- pairing ----------

def test_pair_code_claims_the_bot(tmp_path, monkeypatch):
    db, ident = make_ident(tmp_path, monkeypatch)
    dp, bot, session = build(db, ident)
    code = ident.new_pair_code()
    assert re.fullmatch(r"\d{6}", code)
    feed(dp, bot, message=dm_message(f"/start {code}", uid=42))
    assert ident.owner_id == 42 and ident.pair_code is None
    assert "владелец" in texts(session)[0]


def test_wrong_or_missing_code_is_refused_politely(tmp_path, monkeypatch):
    db, ident = make_ident(tmp_path, monkeypatch)
    dp, bot, session = build(db, ident)
    code = ident.new_pair_code()
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
    feed(dp, bot, message=dm_message(f"/start {wrong}", uid=42))
    feed(dp, bot, message=dm_message("/start", uid=42))
    feed(dp, bot, message=dm_message("привет", uid=42))
    assert ident.owner_id is None
    assert texts(session) == ["Это личный бот дневника."] * 3
    assert all(code not in t for t in texts(session))


def test_claiming_locks_after_five_wrong_codes(tmp_path, monkeypatch):
    db, ident = make_ident(tmp_path, monkeypatch)
    code = ident.new_pair_code()
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
    assert not any(ident.try_claim(wrong, 1) for _ in range(5))
    assert not ident.try_claim(code, 1) and ident.owner_id is None


def test_env_owner_wins_over_database(tmp_path, monkeypatch):
    db, ident = make_ident(tmp_path, monkeypatch, owner=7, channel=CHANNEL)
    db.set("owner_id", "99")
    db.set("channel_id", "-100555")
    assert (ident.owner_id, ident.channel_id) == (7, CHANNEL)
    assert ident.new_pair_code() and not ident.try_claim(ident.pair_code, 42)  # an owner is known already


def test_stranger_cannot_bind_a_channel(tmp_path, monkeypatch):
    db, ident = make_ident(tmp_path, monkeypatch, owner=7)
    dp, bot, session = build(db, ident)

    def promotion(uid):
        return ChatMemberUpdated(
            chat=Chat(id=-100777, type="channel", title="C"), from_user=user(uid), date=NOW,
            old_chat_member=ChatMemberLeft(user=user(BOT_ID)),
            new_chat_member=ChatMemberAdministrator(
                user=user(BOT_ID), can_be_edited=False, is_anonymous=False, can_manage_chat=True,
                can_delete_messages=True, can_manage_video_chats=False, can_restrict_members=False,
                can_promote_members=False, can_change_info=False, can_invite_users=False, can_post_stories=False,
                can_edit_stories=False, can_delete_stories=False, can_post_messages=True))

    feed(dp, bot, my_chat_member=promotion(99))
    assert ident.channel_id is None
    feed(dp, bot, my_chat_member=promotion(7))
    assert ident.channel_id == -100777


def test_post_from_unbound_channel_is_ignored(tmp_path, monkeypatch):
    db, ident = make_ident(tmp_path, monkeypatch, owner=7)
    dp, bot, _ = build(db, ident)
    post = Message(message_id=1, date=NOW, chat=Chat(id=-100777, type="channel"), text=LOG)
    feed(dp, bot, channel_post=post)
    assert ident.channel_id is None and db.post(1) is None


def test_owner_binds_channel_by_forward_with_both_admins(tmp_path, monkeypatch):
    db, ident = make_ident(tmp_path, monkeypatch, owner=7)
    dp, bot, session = build(db, ident)
    feed(dp, bot, message=forwarded())
    assert ident.channel_id is None and "не админ" in texts(session)[-1]
    session.admins |= {(CHANNEL, BOT_ID), (CHANNEL, 7)}
    feed(dp, bot, message=forwarded())
    assert ident.channel_id == CHANNEL and db.post(55) is not None


# ---------- forwarded posts ----------

def test_forwarded_channel_post_is_ingested_once(tmp_path, monkeypatch):
    db, ident = make_ident(tmp_path, monkeypatch, owner=7, channel=CHANNEL)
    dp, bot, session = build(db, ident)
    feed(dp, bot, message=forwarded())
    feed(dp, bot, message=forwarded())
    assert db.post(55) is not None and db.post_counts() == {"log": 1}
    assert db.counts()["sets"] == 4
    assert "Пост учтён." in texts(session)


def test_forward_from_another_channel_is_not_ingested(tmp_path, monkeypatch):
    db, ident = make_ident(tmp_path, monkeypatch, owner=7, channel=CHANNEL)
    dp, bot, session = build(db, ident)
    feed(dp, bot, message=forwarded(chat_id=-100999))
    assert db.post(55) is None and "/rebind" in texts(session)[-1]


# ---------- /rebind ----------

def test_rebind_replaces_the_channel_after_confirmation(tmp_path, monkeypatch):
    db, ident = make_ident(tmp_path, monkeypatch, owner=7)
    ident.set_channel(CHANNEL)
    dp, bot, session = build(db, ident)
    session.admins |= {(-100999, BOT_ID), (-100999, 7)}
    feed(dp, bot, message=dm_message("/rebind"))
    feed(dp, bot, message=forwarded(chat_id=-100999))
    assert ident.channel_id == CHANNEL  # nothing changes before the button
    feed(dp, bot, callback_query=callback("rebind:-100999"))
    assert ident.channel_id == -100999
    assert session.of(AnswerCallbackQuery)


def test_rebind_needs_the_command_and_admin_rights(tmp_path, monkeypatch):
    db, ident = make_ident(tmp_path, monkeypatch, owner=7)
    ident.set_channel(CHANNEL)
    dp, bot, session = build(db, ident)
    feed(dp, bot, callback_query=callback("rebind:-100999"))
    assert ident.channel_id == CHANNEL
    feed(dp, bot, message=dm_message("/rebind"))
    feed(dp, bot, message=forwarded(chat_id=-100999))  # nobody is admin there
    assert ident.channel_id == CHANNEL and "не админ" in texts(session)[-1]


# ---------- alerts, scheduler ----------

class FlakyBot(FakeBot):
    def __init__(self, exc=None):
        super().__init__()
        self.exc, self.attempts = exc, 0

    async def send_message(self, chat_id, text, **kw):
        self.attempts += 1
        if self.exc is not None:
            raise self.exc
        return await super().send_message(chat_id, text, **kw)


def net_error():
    return TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="down")


def test_alert_is_stamped_only_after_it_was_delivered(env):
    db, ident, alerts, _ = env
    bot = FlakyBot(net_error())
    run(alerts.report(bot, ident, "job", RuntimeError("boom")))
    assert alerts.sent == {}
    bot.exc = None
    run(alerts.report(bot, ident, "job", RuntimeError("boom")))
    run(alerts.report(bot, ident, "job", RuntimeError("boom")))
    assert len(bot.sent) == 1 and "job:RuntimeError" in alerts.sent


def test_scheduler_retries_after_a_network_error(env):
    from bot.scheduler import tick
    db, ident, alerts, _ = env
    bot = FlakyBot(net_error())
    evening = datetime(2026, 10, 27, 21, 40, tzinfo=MSK)
    assert "nudge" not in run(tick(bot, db, ident, alerts, evening))
    assert db.get("job:nudge") is None  # unmarked: it will be tried again
    assert "nudge" not in run(tick(bot, db, ident, alerts, evening + timedelta(seconds=30)))
    assert bot.attempts == 1  # backing off
    bot.exc = None
    assert "nudge" in run(tick(bot, db, ident, alerts, evening + timedelta(minutes=3)))
    assert db.get("job:nudge") == "2026-10-27"


def test_scheduler_gives_up_after_six_attempts_and_alerts_once(env):
    from bot.scheduler import MAX_ATTEMPTS, tick
    db, ident, alerts, _ = env
    bot = FlakyBot(net_error())
    start = datetime(2026, 10, 27, 21, 31, tzinfo=MSK)
    for i in range(MAX_ATTEMPTS):
        run(tick(bot, db, ident, alerts, start + timedelta(minutes=11 * i)))
    assert db.get_json("jobtry:nudge")["n"] == MAX_ATTEMPTS
    assert db.get("job:nudge") == "2026-10-27"  # marked for good
    before = bot.attempts
    run(tick(bot, db, ident, alerts, start + timedelta(minutes=80)))
    assert bot.attempts == before


def test_job_with_a_bad_request_is_alerted_not_retried(env):
    from bot.scheduler import tick
    db, ident, alerts, _ = env
    bad = TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="chat not found")
    bot = FlakyBot(bad)
    run(tick(bot, db, ident, alerts, datetime(2026, 10, 27, 21, 40, tzinfo=MSK)))
    assert db.get("job:nudge") == "2026-10-27" and db.get_json("jobtry:nudge") is None


def test_dm_failure_is_not_a_success_for_strict_callers(env):
    from bot.common import dm
    _, ident, _, _ = env
    bot = FlakyBot(net_error())
    assert run(dm(bot, ident, "x")) is False
    with pytest.raises(TelegramNetworkError):
        run(dm(bot, ident, "x", strict=True))


# ---------- heartbeat, downtime ----------

def test_corrupted_heartbeat_means_unknown_downtime(tmp_path):
    from bot import health
    from bot.__main__ import downtime_notice
    beat = tmp_path / "heartbeat"
    for junk in ("", "not a date", "2026-13-45", "\x00\x01"):
        beat.write_text(junk)
        assert health.downtime_minutes(beat) is None
    assert health.downtime_minutes(tmp_path / "missing") is None
    assert downtime_notice(None) is None


def test_heartbeat_is_written_atomically_and_read_back(tmp_path):
    from bot import health
    beat = tmp_path / "heartbeat"
    now = datetime(2026, 10, 27, 12, 0, tzinfo=MSK)
    health.write_heartbeat(beat, now)
    assert not (tmp_path / "heartbeat.tmp").exists()
    assert health.downtime_minutes(beat, now + timedelta(minutes=20)) == pytest.approx(20)


def test_downtime_notice_wording():
    from bot.__main__ import downtime_notice
    assert downtime_notice(10) is None
    assert "прочитаю сам" in downtime_notice(60)
    long = downtime_notice(30 * 60)
    assert "прочитаю сам" not in long and "Перешли" in long and "/import" in long


def test_full_disk_does_not_stop_the_jobs(env, monkeypatch):
    import bot.scheduler as sch
    db, ident, alerts, bot = env
    ran = []

    async def fake_tick(*a, **kw):
        ran.append(1)
        raise asyncio.CancelledError

    monkeypatch.setattr(sch, "heartbeat", lambda db: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(sch, "tick", fake_tick)
    with pytest.raises(asyncio.CancelledError):
        run(sch.scheduler_loop(bot, db, ident, alerts))
    assert ran == [1]


def test_heartbeat_pauses_while_polling_is_dead(env, monkeypatch):
    import bot.scheduler as sch
    from bot import health
    db, ident, alerts, bot = env
    poll = health.Health()
    poll.last_poll -= 600
    beats = []

    async def fake_tick(*a, **kw):
        raise asyncio.CancelledError

    monkeypatch.setattr(sch, "heartbeat", lambda db: beats.append(1))
    monkeypatch.setattr(sch, "tick", fake_tick)
    with pytest.raises(asyncio.CancelledError):
        run(sch.scheduler_loop(bot, db, ident, alerts, poll))
    assert beats == []


def test_watchdog_exits_after_silence():
    from bot import health
    poll = health.Health()
    poll.last_poll -= 1000
    codes, alerts = [], []

    async def alert(text):
        alerts.append(text)

    run(health.watchdog(None, poll, alert, limit=900, every=0, exit_fn=codes.append))
    assert codes == [1] and alerts


def test_poll_tracker_records_only_successful_get_updates():
    from aiogram.methods import GetUpdates
    from bot import health
    poll = health.Health()
    poll.last_poll -= 500
    tracker = health.PollTracker(poll)

    async def ok(bot, method):
        return []

    async def boom(bot, method):
        raise TelegramNetworkError(method=method, message="down")

    run(tracker(ok, None, SendMessage(chat_id=1, text="x")))
    assert not poll.polling_ok()
    with pytest.raises(TelegramNetworkError):
        run(tracker(boom, None, GetUpdates()))
    assert not poll.polling_ok()
    run(tracker(ok, None, GetUpdates()))
    assert poll.polling_ok()


def test_unwritable_data_dir_exits_with_code_2(tmp_path):
    from bot import health
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(SystemExit) as e:
        health.ensure_writable(blocker / "data")
    assert e.value.code == 2


# ---------- restore ----------

def donor(tmp_path, owner="7"):
    other = Db(tmp_path / "other.db")
    ingest_post(other, 900, datetime(2026, 9, 1, tzinfo=timezone.utc), LOG)
    ingest_post(other, 901, datetime(2026, 9, 3, tzinfo=timezone.utc), "Приседания – 4x8 80кг")
    if owner:
        other.set("owner_id", owner)
    path = backup.snapshot(other, tmp_path / "copy.db")
    other.conn.close()
    return path


def test_restore_from_a_valid_file(tmp_path, env):
    from bot.owner import restore
    db, *_ = env
    db.set("owner_id", "7")
    path = donor(tmp_path)
    assert not (tmp_path / "copy.db-wal").exists()
    found = restore.validate(path, 7)
    assert (found.posts, found.sessions) == (2, 2)
    restore.prepare(path)
    got = restore.apply(db, path)
    assert (got.posts, got.sessions) == (2, 2)
    assert db.post(1) is None and db.post(900) is not None
    assert list((db.path.parent / "backups").glob("pre-restore-*.db"))
    fresh = Db(db.path)  # what the next start sees: no stale -wal undoing the restore
    assert fresh.post(900) is not None and fresh.get("owner_id") == "7"


def test_restore_keeps_the_owner_of_an_unpaired_file(tmp_path, env):
    from bot.owner import restore
    db, *_ = env
    db.set("owner_id", "7")
    path = donor(tmp_path, owner="")
    restore.prepare(path)
    restore.apply(db, path)
    assert db.get("owner_id") == "7"


def test_restore_rejects_broken_and_foreign_files(tmp_path):
    from bot.owner.restore import RestoreError, validate
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(RestoreError):
        validate(junk, 7)
    empty = tmp_path / "empty.db"
    import sqlite3
    sqlite3.connect(str(empty)).execute("CREATE TABLE x(a)").connection.close()
    with pytest.raises(RestoreError, match="не база дневника"):
        validate(empty, 7)
    foreign = donor(tmp_path, owner="99")
    with pytest.raises(RestoreError, match="другому владельцу"):
        validate(foreign, 7)


def test_restore_button_applies_the_pending_file(tmp_path, monkeypatch):
    from bot import pending
    from bot.owner import restore
    db, ident = make_ident(tmp_path, monkeypatch, owner=7)
    dp, bot, session = build(db, ident)
    path = donor(tmp_path)
    restore.prepare(path)
    path.replace(restore.pending_path(db))
    feed(dp, bot, callback_query=callback("restore:yes"))  # nothing was offered: stale file is dropped
    assert db.post(900) is None and not restore.pending_path(db).exists()
    path = donor(tmp_path)
    restore.prepare(path)
    path.replace(restore.pending_path(db))
    pending.put(db, "restore", {"posts": 2, "sessions": 2})
    feed(dp, bot, callback_query=callback("restore:yes"))
    assert db.post(900) is not None and not restore.pending_path(db).exists()
    assert any("Восстановил" in t for t in texts(session))


def test_zip_import_reads_only_export_members_and_caps_size(tmp_path):
    import zipfile
    from bot.owner import data
    z = tmp_path / "e.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("x/result.json", "{}")
        zf.writestr("../evil.txt", "no")
        zf.writestr("photos/big.jpg", "x" * 10)
    out = tmp_path / "out"
    out.mkdir()
    assert data.extract_export(z, out, "e.zip") == 1
    assert [p.name for p in out.iterdir()] == ["result.json"]
    old = data.MAX_UNPACKED
    data.MAX_UNPACKED = 1
    try:
        with pytest.raises(ValueError):
            data.extract_export(z, out, "e.zip")
    finally:
        data.MAX_UNPACKED = old


def test_import_runs_in_its_own_connection(tmp_path, env):
    import json
    from bot.owner import data
    db, *_ = env
    folder = tmp_path / "exp"
    folder.mkdir()
    (folder / "result.json").write_text(json.dumps({"messages": [
        {"id": 500, "type": "message", "date": "2026-09-01T10:00:00", "text": LOG}]}), encoding="utf-8")
    report = asyncio.run(asyncio.to_thread(data.import_in_own_connection, db.path, folder))
    assert report.posts == 1 and db.post(500) is not None


# ---------- callbacks ----------

def test_inaccessible_message_gets_a_new_message_and_an_answer(env):
    db, ident, alerts, _ = env
    dp, bot, session = build(db, ident)
    feed(dp, bot, callback_query=callback("later"))
    assert not session.of(EditMessageText)
    assert "подождёт" in texts(session)[-1]
    assert session.of(AnswerCallbackQuery)


def test_not_modified_is_ignored(env):
    db, ident, alerts, _ = env
    dp, bot, session = build(db, ident)
    session.fail = lambda m: (TelegramBadRequest(method=m, message="Bad Request: message is not modified")
                              if isinstance(m, EditMessageText) else None)
    accessible = Message(message_id=9, date=NOW, chat=Chat(id=7, type="private"), text="x")
    cb = CallbackQuery(id="1", from_user=user(), chat_instance="c", data="later", message=accessible)
    feed(dp, bot, callback_query=cb)
    assert session.of(EditMessageText) and not texts(session)
    assert session.of(AnswerCallbackQuery)


def test_stranger_button_is_still_answered(env):
    db, ident, alerts, _ = env
    dp, bot, session = build(db, ident)
    feed(dp, bot, callback_query=callback("later", uid=99))
    assert session.of(AnswerCallbackQuery) and not texts(session)


def test_typed_workout_stays_when_the_channel_send_fails(env):
    from bot import pending
    db, ident, alerts, _ = env
    dp, bot, session = build(db, ident)
    pending.put(db, "77", LOG)
    session.fail = lambda m: net_error() if isinstance(m, SendMessage) and m.chat_id == ident.channel_id else None
    feed(dp, bot, callback_query=callback("dmlog:77"))
    assert pending.get(db, "77") == LOG and db.post_counts().get("log") is None
    answer = session.of(AnswerCallbackQuery)[-1]
    assert answer.show_alert and "сохранён" in answer.text
    session.fail = lambda m: None
    feed(dp, bot, callback_query=callback("dmlog:77"))
    assert pending.get(db, "77") is None and db.post_counts().get("log") == 1
    assert any("Записал в" in t for t in texts(session))


# ---------- pending, escaping, splitting ----------

def test_pending_entries_expire(tmp_path):
    from bot import pending
    db = Db(tmp_path / "d.db")
    pending.put(db, "1", "fresh")
    pending.put(db, "2", "old")
    db.set_json("pending:2", {"t": datetime.now().timestamp() - 3 * 86400, "v": "old"})
    db.set_json("pending:3", "legacy text without a stamp")
    assert pending.prune(db) == 1
    assert pending.get(db, "1") == "fresh" and pending.get(db, "2") is None
    assert pending.get(db, "3") == "legacy text without a stamp"  # got a stamp, lives on


def test_user_text_in_job_messages_is_escaped(env):
    from bot.scheduler import tick
    db, ident, alerts, bot = env
    db.conn.execute("UPDATE program_day SET title = 'Верх <b>&'")
    db.conn.commit()
    run(tick(bot, db, ident, alerts, datetime(2026, 10, 27, 21, 40, tzinfo=MSK)))
    assert "Верх &lt;b&gt;&amp;" in bot.sent[-1][1]


def test_avoid_list_is_escaped(env):
    db, ident, alerts, _ = env
    from diary import state
    eid = db.add_exercise("Жим <i>", "barbell")
    state.avoid(db, eid)
    dp, bot, session = build(db, ident)
    feed(dp, bot, message=dm_message("/avoid"))
    assert "Жим &lt;i&gt;" in texts(session)[-1]


def test_long_html_is_split_on_lines_without_breaking_tags():
    from bot.common import chunks
    text = "<pre>" + "\n".join("строка &amp; <x " * 3 for _ in range(1)) + "</pre>"
    text = ("<pre>" + "\n".join("жим &amp; присед 4x8 60кг" for _ in range(300)) + "</pre>\n"
            + "• <b>" + "слово " * 400 + "</b>\n#план")
    parts = chunks(text, 600)
    assert len(parts) > 5 and all(len(p) <= 600 for p in parts)
    for p in parts:
        assert p.count("<pre>") == p.count("</pre>") and p.count("<b>") == p.count("</b>")
        assert not re.search(r"&\w*$", p) and not re.search(r"<[^>]*$", p)
    assert parts[-1].endswith("#план")


def test_one_giant_line_never_cuts_an_entity():
    from bot.common import chunks
    parts = chunks("&amp;" * 2000, 300)
    assert all(len(p) <= 300 and p.count("&amp;") * 5 == len(p) for p in parts)


def test_caption_is_shortened_before_escaping():
    from bot import ui
    plain = "<&>" * 600
    cap = ui.caption(plain)
    assert len(cap) <= 1024 and not re.search(r"&\w*$", cap.rstrip("…"))
    assert ui.caption_fits(plain) is None and ui.caption("short <") == "short &lt;"


def test_plan_in_the_channel_is_only_the_program(env):
    from bot.common import post_plan
    from diary import clock, state
    from diary.parse import parse_post
    db, ident, alerts, bot = env
    assert run(post_plan(bot, db, ident))
    last = bot.sent[-1][1]
    assert last.startswith("НЕДЕЛЯ 1") and "Почему" not in last and "•" not in last and "<" not in last
    assert parse_post(last).kind != "bot"  # the owner copies it and fills it in: that copy is a workout
    assert state.plan_note(db, clock.today()) is not None


def test_status_shows_package_version():
    from bot.owner.settings import version_text
    import diary
    assert version_text("dev") == diary.__version__
    assert version_text("abc123") == f"{diary.__version__} [abc123]"


# ---------- workouts edited during the session ----------

def channel_post(text, mid=500, edit_date=None):
    return Message(message_id=mid, date=NOW, chat=Chat(id=CHANNEL, type="channel"), text=text, edit_date=edit_date)


def reactions(session):
    from aiogram.methods import SetMessageReaction
    return [(c.message_id, c.reaction[0].emoji) for c in session.of(SetMessageReaction)]


def test_edit_with_a_unix_edit_date_is_stored(tmp_path, monkeypatch):
    db, ident = make_ident(tmp_path, monkeypatch, owner=7, channel=CHANNEL)
    dp, bot, session = build(db, ident)
    feed(dp, bot, channel_post=channel_post("Жим штанги лежа – 4x8 60кг"))
    edited = channel_post("Жим штанги лежа – 4x8 62,5кг", edit_date=int(NOW.timestamp()) + 600)
    feed(dp, bot, edited_channel_post=edited)
    assert {r["weight"] for r in db.post_sets(500)} == {62.5}
    assert db.post(500)["edited_utc"] and not any("Ошибка" in t for t in texts(session))


def test_workout_report_waits_until_the_post_is_quiet(tmp_path, monkeypatch):
    from bot.channel import LIVE_KEY, settle_workouts
    from diary import clock
    db, ident = make_ident(tmp_path, monkeypatch, owner=7, channel=CHANNEL)
    dp, bot, session = build(db, ident)
    feed(dp, bot, channel_post=channel_post("Жим штанги лежа – 4x8 60кг\n85кг - тяжело"))
    edited = channel_post("Жим штанги лежа – 4x8 60кг\nТяга 3x10 50кг\n90кг - тяжело",
                          edit_date=int(NOW.timestamp()) + 300)
    feed(dp, bot, edited_channel_post=edited)
    assert reactions(session) == [(500, "✍"), (500, "✍")] and texts(session) == []  # nothing while editing
    assert run(settle_workouts(bot, db, ident, clock.now() + timedelta(minutes=10))) == 0
    assert run(settle_workouts(bot, db, ident, clock.now() + timedelta(minutes=41))) == 1
    said = "\n".join(texts(session))
    assert "90кг - тяжело" in said and "85кг" not in said  # only the final text is reported
    assert reactions(session)[-1] == (500, "🤔") and db.get_json(LIVE_KEY)["500"]["done"]
    assert run(settle_workouts(bot, db, ident, clock.now() + timedelta(minutes=90))) == 0  # once


def test_settle_zero_reports_at_once(tmp_path, monkeypatch):
    db, ident = make_ident(tmp_path, monkeypatch, owner=7, channel=CHANNEL)
    db.set("log_settle", "0")
    dp, bot, session = build(db, ident)
    feed(dp, bot, channel_post=channel_post("Жим штанги лежа – 4x8 60кг"))
    assert reactions(session)[-1] == (500, "👍")
