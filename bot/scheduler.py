"""Timed jobs. Each runs at most once per period and catches up if the bot was down at the time."""
from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Awaitable, Callable

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import FSInputFile
from python_socks import ProxyError

from diary import analytics, backup, charts, goals, program, state, stats
from diary.db import Db
from diary.today import RESTART_AFTER_DAYS, gap_days

from . import health, prefs, ui
from .common import Alerts, Identity, dm, kb, now_msk, post_plan

log = logging.getLogger(__name__)
TICK = 30
MAX_ATTEMPTS = 6    # per job and period; a retryable failure is tried again on a later tick
RETRYABLE = (TelegramNetworkError, TelegramRetryAfter, TelegramServerError, TimeoutError,
             aiohttp.ClientError, ConnectionError, ProxyError)
INACTIVE_DAYS = 7
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь"]


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
    if db.get("morning_posted") != now.date().isoformat():  # a retry after a failed DM must not post twice
        await post_plan(bot, db, ident)
        db.set("morning_posted", now.date().isoformat())
    gap = gap_days(db, now.date())
    if gap and gap > RESTART_AFTER_DAYS:
        await dm(bot, ident, f"Перерыв {gap} дн. Начать цикл с первой недели?", strict=True,
                 reply_markup=kb([("Да, с начала", "restart")]))


async def run_nudge(bot: Bot, db: Db, ident: Identity, now: datetime) -> None:
    day = program.next_day(db)
    if day:
        await dm(bot, ident, f"Сегодня по плану «{html.escape(day['title'])}». Если был в зале — запиши в канал. "
                             "Если нет — ничего, день подождёт.", strict=True,
                 reply_markup=kb([("Пропустить этот день", "skipday"), ("Сделаю позже", "later")]))


async def run_summary(bot: Bot, db: Db, ident: Identity, now: datetime) -> None:
    text = stats.weekly_summary(db, now.date())
    c = analytics.consistency(db, now.date())
    png = await ui.render(charts.weekly, analytics.weekly_series(db, now.date()), c.planned)
    caption = ui.caption_fits(text)
    if png and ident.owner_id and caption is not None:
        await bot.send_photo(ident.owner_id, ui.photo(png, "week"), caption=caption)
    else:
        await dm(bot, ident, html.escape(text), strict=True)
    if summary := goals.summary(db, now.date()):
        await dm(bot, ident, html.escape(summary))


async def run_monthly(bot: Bot, db: Db, ident: Identity, now: datetime) -> None:
    if ident.owner_id:
        prev = (now.date().replace(day=1) - timedelta(days=1))
        await ui.send_analytics(bot, ident.owner_id, db, now.date(), title=f"📊 Итоги: {MONTHS[prev.month - 1]}")
        if summary := goals.summary(db, now.date()):
            await dm(bot, ident, html.escape(summary))


def monthly(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


async def run_inactivity(bot: Bot, db: Db, ident: Identity, now: datetime) -> None:
    last = db.last_session_day()
    gap = gap_days(db, now.date())
    buttons = kb([("Цикл с начала", "restart")]) if gap and gap > RESTART_AFTER_DAYS else None
    await dm(bot, ident, f"{gap} дней без записей. Болеешь или уехал — /pause, и я замолчу. "
                         "Готов вернуться — /plan покажет веса на вкат.", strict=True, reply_markup=buttons)
    db.set("inactivity_for", last or "")  # only after it went out, or a retry would find the job already done


async def run_backup(bot: Bot, db: Db, ident: Identity, now: datetime) -> None:
    path = backup.make_backup(db, now.date())
    if now.weekday() == 0 and prefs.on(db, "backup_send") and ident.owner_id:
        await bot.send_document(ident.owner_id, FSInputFile(path),
                                caption="Еженедельная копия базы. Чтобы восстановить — просто пришли этот файл мне.",
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
    Job("monthly", lambda db: prefs.hhmm(db, "summary_time"), timedelta(hours=3), monthly,
        lambda db, now: prefs.on(db, "monthly") and now.day == 1 and db.last_session_day() is not None, run_monthly),
    Job("inactivity", lambda db: (12, 0), timedelta(hours=10), daily,
        lambda db, now: not state.is_paused(db, now.date()) and inactive(db, now), run_inactivity),
    Job("backup", lambda db: (4, 0), timedelta(hours=20), daily, lambda db, now: True, run_backup),
]


def due(job: Job, db: Db, now: datetime) -> bool:
    period = job.period(now.date())
    if db.get(f"job:{job.name}") == period:
        return False
    tries = db.get_json(f"jobtry:{job.name}")
    if tries and tries["period"] == period and now.timestamp() < tries["next"]:
        return False  # backing off after a failed attempt
    hh, mm = job.at(db)
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return start <= now < start + job.window and job.when(db, now)


def heartbeat(db: Db) -> None:
    health.write_heartbeat(health.heartbeat_path(db.path))


async def retry_later(job: Job, db: Db, ident: Identity, alerts: Alerts, bot: Bot, now: datetime,
                      exc: BaseException) -> None:
    """Unmark a job that failed on a network error so a later tick retries it; give up after MAX_ATTEMPTS."""
    period = job.period(now.date())
    tries = db.get_json(f"jobtry:{job.name}")
    n = tries["n"] + 1 if tries and tries["period"] == period else 1
    delay = exc.retry_after if isinstance(exc, TelegramRetryAfter) else min(60 * n, 600)
    db.set_json(f"jobtry:{job.name}", {"period": period, "n": n, "next": now.timestamp() + delay})
    if n >= MAX_ATTEMPTS:
        await alerts.report(bot, ident, f"задача {job.name}", RuntimeError(f"сдаюсь после {n} попыток: {exc}"))
        return
    log.warning("job %s failed (%s), attempt %d of %d", job.name, exc, n, MAX_ATTEMPTS)
    db.unset(f"job:{job.name}")


async def tick(bot: Bot, db: Db, ident: Identity, alerts: Alerts, now: datetime | None = None) -> list[str]:
    now = now or now_msk()
    ran = []
    for job in JOBS:
        try:
            if due(job, db, now):
                db.set(f"job:{job.name}", job.period(now.date()))  # mark first: a failing job must not loop
                await job.run(bot, db, ident, now)
                ran.append(job.name)
        except RETRYABLE as e:
            await retry_later(job, db, ident, alerts, bot, now, e)
        except Exception as e:  # noqa: BLE001 - one broken job must not stop the others
            await alerts.report(bot, ident, f"задача {job.name}", e)
    return ran


async def scheduler_loop(bot: Bot, db: Db, ident: Identity, alerts: Alerts, poll: health.Health | None = None) -> None:
    while True:
        if poll is None or poll.polling_ok():  # a deaf bot must look unhealthy
            try:
                heartbeat(db)
            except Exception as e:  # noqa: BLE001 - a full disk must not stop the jobs
                log.error("heartbeat not written: %s", e)
        try:
            await tick(bot, db, ident, alerts)
        except Exception as e:  # noqa: BLE001
            await alerts.report(bot, ident, "планировщик", e)
        await asyncio.sleep(TICK)
