"""Persistent keyboard and chart delivery."""
from __future__ import annotations

import asyncio
import html
from datetime import date

from aiogram import Bot
from aiogram.types import BufferedInputFile, InputMediaPhoto, KeyboardButton, ReplyKeyboardMarkup

from diary import analytics, charts, clock, goals, stats
from diary.db import Db

from .common import kb, send_long

BTN_PLAN = "📋 План"
BTN_LIGHT = "😵 Лёгкий"
BTN_SHORT = "⏱ Коротко"
BTN_SWAP = "🔁 Замена"
BTN_PROGRESS = "📈 Прогресс"
BTN_ANALYTICS = "📊 Аналитика"
BTN_PAUSE = "⏸ Пауза"
BTN_MORE = "⚙️ Ещё"
BUTTONS = {BTN_PLAN, BTN_LIGHT, BTN_SHORT, BTN_SWAP, BTN_PROGRESS, BTN_ANALYTICS, BTN_PAUSE, BTN_MORE}
CAPTION = 1024  # Telegram's limit for a photo caption, counted after parsing
KEYBOARD_VERSION = "1"  # bump to re-send the keyboard to the owner after a layout change

MAIN = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_PLAN), KeyboardButton(text=BTN_LIGHT), KeyboardButton(text=BTN_SHORT)],
        [KeyboardButton(text=BTN_SWAP), KeyboardButton(text=BTN_PROGRESS), KeyboardButton(text=BTN_ANALYTICS)],
        [KeyboardButton(text=BTN_PAUSE), KeyboardButton(text=BTN_MORE)],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Тренировка или кнопка",
)


def caption_fits(text: str) -> str | None:
    """The escaped text if it fits a photo caption as is, else None."""
    out = html.escape(text)
    return out if len(out) <= CAPTION else None


def caption(text: str) -> str:
    """Escaped caption within the limit: the plain text is shortened first, so no entity or tag is ever cut."""
    if (out := caption_fits(text)) is not None:
        return out
    cut = text[:CAPTION]
    while len(out := html.escape(cut.rstrip()) + "…") > CAPTION:
        cut = cut[:-max(1, (len(out) - CAPTION) // 6)]
    return out


def photo(png: bytes, name: str) -> BufferedInputFile:
    return BufferedInputFile(png, filename=f"{name}.png")


async def render(fn, *args) -> bytes | None:
    """Charts are CPU work: draw in a thread so polling never stalls. Data is prepared by the caller."""
    return await asyncio.to_thread(fn, *args)


def progress_menu(db: Db, today: date):
    lifts = analytics.main_lifts(db, today)
    rows = [[(name[:40], f"chart:{eid}")] for eid, name in lifts]
    rows.append([("⚖️ Вес тела", "chart:body"), ("🎯 КПД", "chart:kpd"), ("🏁 Цели", "goals")])
    return kb(*rows)


async def send_strength(bot: Bot, chat_id: int, db: Db, eid: int) -> None:
    name = db.exercise(eid)["name"]
    target, line, plain = None, None, stats.progress_text(db, eid)
    if g := goals.lift_goal(db, eid):
        f = goals.forecast(db, g, clock.today())
        target, line = g.target, goals.projection(f, clock.today())
        plain += "\n\n" + goals.describe(f, clock.today())
    png = await render(charts.strength, name, analytics.strength_points(db, eid), target, line)
    if png:
        await bot.send_photo(chat_id, photo(png, "strength"), caption=caption(plain))
        if caption_fits(plain) is None:
            await send_long(bot, chat_id, html.escape(plain))  # the caption was shortened: the rest follows
    else:
        await send_long(bot, chat_id, html.escape(plain) + "\nДля графика нужно хотя бы две тренировки с весом.")


async def send_body(bot: Bot, chat_id: int, db: Db) -> None:
    png = await render(charts.bodyweight, analytics.bodyweight_points(db))
    plain = stats.measurements_text(db)
    if png:
        await bot.send_photo(chat_id, photo(png, "bodyweight"), caption=caption(plain))
        if caption_fits(plain) is None:
            await send_long(bot, chat_id, html.escape(plain))
    else:
        await send_long(bot, chat_id, html.escape(plain) + "\nГрафик веса появится, когда будет хотя бы два поста вида «70,6кг».")


async def send_kpd(bot: Bot, chat_id: int, db: Db) -> None:
    series = [k for k in analytics.kpd_series(db) if k.score is not None]
    if not series:
        await bot.send_message(chat_id, "КПД пока не посчитать: нужна программа и пара тренировок по ней.")
        return
    last = "\n".join(f"{k.day:%d.%m} {html.escape(k.title)}: {html.escape(k.text())}" for k in series[-5:])
    explain = ("\n\n<i>КПД = выполнение плана (40%) + подходы, где добрал нижнюю границу повторов (40%) "
               "+ прибавка к тому же дню прошлого цикла (20%). Разгрузку не штрафую за отсутствие прибавки.</i>")
    png = await render(charts.kpd, analytics.kpd_points(db))
    if png:
        await bot.send_photo(chat_id, photo(png, "kpd"))
    await bot.send_message(chat_id, last + explain)


async def analytics_pack(db: Db, today: date) -> list[bytes]:
    c = analytics.consistency(db, today, 26)
    prog = db.active_program()
    plan_from = date.fromisoformat(prog["day"]) if prog else None
    jobs = [
        (charts.consistency, (c.training_days, analytics.planned_weekdays(db), today, 26, "", plan_from, c.paused)),
        (charts.weekly, (analytics.weekly_series(db, today), c.planned)),
        (charts.volume, (analytics.weekly_volume(db, today, by="muscle"), analytics.VOLUME_ZONE)),
    ]
    out = []
    for fn, args in jobs:
        if png := await render(fn, *args):
            out.append(png)
    return out


async def send_analytics(bot: Bot, chat_id: int, db: Db, today: date, title: str = "📊 Аналитика") -> None:
    text = f"<b>{title}</b>\n" + html.escape(analytics.report_text(db, today))
    pngs = await analytics_pack(db, today)
    if pngs:
        await bot.send_media_group(chat_id, [InputMediaPhoto(media=photo(p, f"a{i}")) for i, p in enumerate(pngs)])
    await send_long(bot, chat_id, text, reply_markup=kb([("📈 Сила", "progressmenu"), ("🎯 КПД", "chart:kpd"),
                                                         ("⚖️ Вес", "chart:body"), ("🏁 Цели", "goals")]))
