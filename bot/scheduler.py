"""Timed jobs. Each runs at most once per period and catches up if the bot was down at the time."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.types import FSInputFile

from diary import backup, program, state, stats
from diary.db import Db
from diary.today import RESTART_AFTER_DAYS, gap_days

from . import prefs
from .common import Alerts, Identity, dm, kb, now_msk, post_plan

log = logging.getLogger(__name__)
TICK = 30
INACTIVE_DAYS = 7


@dataclass
class Job:
    name: str
    at: Callable[[Db], tuple[int, int]]       # local time it becomes due
    window: timedelta                         # how late it may still run after a downtime
    period: Callable[[date], str]             # once per this key
    when: Callable[[Db, datetime], bool]      # extra conditions
    run: Callable[[Bot, Db, Identity, datetime], Awaitable[None]]


def daily(d: date) -> str:
    return d.isoformat()


def weekly(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def training_weekday(db: Db, now: datetime) -> bool:
    prog = db.active_program()
    if not prog:
        return False
    return now.weekday() in {d["weekday"] for d in db.program_days(prog["id"]) if d["weekday"] is not None}


def can_nag(db: Db, now: datetime) -> bool:
    return not state.is_paused(db, now.date()) and db.active_program() is not None


async def run_morning(bot: Bot, db: Db, ident: Identity, now: datetime) -> None:
    await post_plan(bot, db, ident)
    gap = gap_days(db, now.date())
    if gap and gap > RESTART_AFTER_DAYS:
        await dm(bot, ident, f"Перерыв {gap} дн. Начать цикл с первой недели?",
                 reply_markup=kb([("Да, с начала", "restart")]))


async def run_nudge(bot: Bot, db: Db, ident: Identity, now: datetime) -> None:
    day = program.next_day(db)
    if day:
        await dm(bot, ident, f"Сегодня по плану «{day['title']}». Если был в зале — запиши в канал. "
                             "Если нет — ничего, день подождёт.",
                 reply_markup=kb([("Пропустить этот день", "skipday"), ("Сделаю позже", "later")]))


async def run_summary(bot: Bot, db: Db, ident: Identity, now: datetime) -> None:
    await dm(bot, ident, stats.weekly_summary(db, now.date()))


async def run_inactivity(bot: Bot, db: Db, ident: Identity, now: datetime) -> None:
    last = db.last_session_day()
    db.set("inactivity_for", last or "")
    gap = gap_days(db, now.date())
    buttons = kb([("Цикл с начала", "restart")]) if gap and gap > RESTART_AFTER_DAYS else None
    await dm(bot, ident, f"{gap} дней без записей. Болеешь или уехал — /pause, и я замолчу. "
                         "Готов вернуться — /plan покажет веса на вкат.", reply_markup=buttons)


async def run_backup(bot: Bot, db: Db, ident: Identity, now: datetime) -> None:
    path = backup.make_backup(db, now.date())
    if now.weekday() == 0 and prefs.on(db, "backup_send") and ident.owner_id:
        await bot.send_document(ident.owner_id, FSInputFile(path), caption="Еженедельная копия базы дневника.",
                                disable_notification=True)


def inactive(db: Db, now: datetime) -> bool:
    last = db.last_session_day()
    if not last or db.get("inactivity_for") == last:
        return False
    return (now.date() - date.fromisoformat(last)).days >= INACTIVE_DAYS


JOBS = [
    Job("morning", lambda db: prefs.hhmm(db, "reminder_time"), timedelta(hours=2), daily,
        lambda db, now: prefs.on(db, "reminder") and can_nag(db, now) and training_weekday(db, now)
        and not db.has_session_on(now.date().isoformat()), run_morning),
    Job("nudge", lambda db: prefs.hhmm(db, "nudge_time"), timedelta(hours=2), daily,
        lambda db, now: prefs.on(db, "nudge") and can_nag(db, now) and training_weekday(db, now)
        and not db.has_session_on(now.date().isoformat()), run_nudge),
    Job("summary", lambda db: prefs.hhmm(db, "summary_time"), timedelta(hours=3), weekly,
        lambda db, now: prefs.on(db, "summary") and now.weekday() == 6, run_summary),
    Job("inactivity", lambda db: (12, 0), timedelta(hours=10), daily,
        lambda db, now: not state.is_paused(db, now.date()) and inactive(db, now), run_inactivity),
    Job("backup", lambda db: (4, 0), timedelta(hours=20), daily, lambda db, now: True, run_backup),
]


def due(job: Job, db: Db, now: datetime) -> bool:
    if db.get(f"job:{job.name}") == job.period(now.date()):
        return False
    hh, mm = job.at(db)
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return start <= now < start + job.window and job.when(db, now)


def heartbeat(db: Db) -> None:
    Path(db.path.parent / "heartbeat").write_text(now_msk().isoformat())


async def tick(bot: Bot, db: Db, ident: Identity, alerts: Alerts, now: datetime | None = None) -> list[str]:
    now = now or now_msk()
    ran = []
    for job in JOBS:
        try:
            if due(job, db, now):
                db.set(f"job:{job.name}", job.period(now.date()))  # mark first: a failing job must not loop
                await job.run(bot, db, ident, now)
                ran.append(job.name)
        except Exception as e:  # noqa: BLE001 - one broken job must not stop the others
            await alerts.report(bot, ident, f"задача {job.name}", e)
    return ran


async def scheduler_loop(bot: Bot, db: Db, ident: Identity, alerts: Alerts) -> None:
    while True:
        try:
            heartbeat(db)
            await tick(bot, db, ident, alerts)
        except Exception as e:  # noqa: BLE001
            await alerts.report(bot, ident, "планировщик", e)
        await asyncio.sleep(TICK)
