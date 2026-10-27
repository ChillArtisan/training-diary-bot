"""Preferences and the status report."""
from __future__ import annotations

import html
import time
from datetime import timedelta

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import diary
from diary import program, state
from diary.recommend import fmt_num

from .. import prefs
from .ctx import Ctx

STARTED = time.monotonic()


def version_text(build: str) -> str:
    """Package version, plus the image build when there is one: «1.2.0 [20261027-ab12]»."""
    return diary.__version__ + (f" [{build}]" if build and build != "dev" else "")


def register(router: Router, ctx: Ctx) -> None:
    db, ident = ctx.db, ctx.ident

    @router.message(Command("settings"))
    async def on_settings(message: Message) -> None:
        rows = [f"<code>{k}</code> = {html.escape(prefs.get(db, k) or '—')} · {why}"
                for k, (_, _, why) in prefs.PREFS.items()]
        bw = db.bodyweight()
        rows.append(f"\nвес тела сейчас: {fmt_num(bw)} кг" if bw else "\nвес тела неизвестен")
        await message.answer("\n".join(rows) + "\n\nМенять: /set ключ значение, например /set reminder_time 07:30")

    @router.message(Command("set"))
    async def on_set(message: Message, command: CommandObject) -> None:
        parts = (command.args or "").split(maxsplit=1)
        if len(parts) != 2 or parts[0] not in prefs.PREFS:
            await message.answer("Ключи: " + ", ".join(prefs.PREFS))
            return
        value = prefs.validate(parts[0], parts[1])
        if value is None:
            await message.answer(f"Не подходит. {parts[0]}: {prefs.PREFS[parts[0]][2]}")
            return
        db.set(parts[0], value)
        await message.answer(f"{parts[0]} = {html.escape(value)}")

    @router.message(Command("status"))
    async def on_status(message: Message, bot: Bot) -> None:
        c, last = db.counts(), db.last_post()
        prog, nxt = db.active_program(), program.next_day(db)
        uptime = timedelta(seconds=int(time.monotonic() - STARTED))
        channel = "не подключён"
        if ident.channel_id:
            try:
                ch = await bot.get_chat(ident.channel_id)
                me = await bot.get_chat_member(ident.channel_id, bot.id)
                ok = me.status == "administrator" and getattr(me, "can_post_messages", False)
                channel = f"«{html.escape(ch.title or '')}» ({'права в порядке' if ok else '⚠️ нет прав админа'})"
            except TelegramAPIError as e:
                channel = f"⚠️ недоступен: {html.escape(str(e)[:100])}"
        size = db.path.stat().st_size / 1024 if db.path.exists() else 0
        until = state.paused_until(db)
        rows = [
            f"Версия {version_text(ident.cfg.version)}, работает {uptime}",
            f"Канал: {channel}",
            f"Постов: {sum(db.post_counts().values())}, тренировок: {c['sessions']}, подходов: {c['sets']}",
            f"Последний пост: {last['day'] if last else '—'}",
            f"Программа: {html.escape(prog['name']) if prog else '—'}; следующий: {html.escape(nxt['title']) if nxt else '—'}",
            f"Пауза до {until:%d.%m.%Y}" if until and state.is_paused(db, ctx.today()) else "Пауза: нет",
            f"Бэкап: {db.get('last_backup') or 'ещё не было'}; база {size:.0f} КБ",
            f"Вопросов по названиям: {len(db.open_suggestions())}",
        ]
        await message.answer("\n".join(rows))
