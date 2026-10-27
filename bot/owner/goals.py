"""Goals: «/goal жим лёжа 100 к 01.06» — set, list with a forecast, drop."""
from __future__ import annotations

import html

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from diary import goals

from .. import ui
from ..common import find_exercise, kb, send_long
from .ctx import Ctx, chat_id_of, edit

USAGE = ("Цель — и я посчитаю, когда она будет по твоему темпу:\n"
         "/goal жим лёжа 100 — максимум на раз\n"
         "/goal присед 100x5 к 01.06 — вес на 5 повторов к сроку\n"
         "/goal вес 75 к лету — вес тела\n"
         "Срок можно так: «к 01.06», «к лету», «через 3 месяца».")


def goals_view(db, today) -> tuple[str, object]:
    gs = goals.all_goals(db)
    if not gs:
        return USAGE, None
    text = html.escape(goals.summary(db, today))
    buttons = [[(f"✖ {g.name[:30]}", f"goaldel:{g.id}")] for g in gs]
    return text + "\n\n<i>Новая цель на то же упражнение заменит старую.</i>", kb(*buttons)


def register(router: Router, ctx: Ctx) -> None:
    db, today = ctx.db, ctx.today

    @router.message(Command("goal", "goals"))
    async def on_goal(message: Message, command: CommandObject, bot: Bot) -> None:
        if not command.args:
            text, markup = goals_view(db, today())
            await send_long(bot, message.chat.id, text, reply_markup=markup)
            return
        req = goals.parse_goal(command.args, today())
        if req is None:
            await message.answer("Не понял цель.\n\n" + USAGE)
            return
        eid = None
        if req.kind == "lift":
            eid = find_exercise(db, req.name)
            if eid is None:
                await message.answer(f"Не нашёл упражнение «{html.escape(req.name)}». Названия — в /stats.")
                return
        g = goals.set_goal(db, today(), req.kind, req.target, eid, req.weight, req.reps, req.deadline)
        if eid is not None:
            await ui.send_strength(bot, message.chat.id, db, eid)  # the chart with the goal line and the forecast
        else:
            await message.answer(html.escape(goals.describe(goals.forecast(db, g, today()), today())))

    @router.callback_query(F.data == "goals")
    async def cb_goals(cb: CallbackQuery, bot: Bot) -> None:
        await cb.answer()
        text, markup = goals_view(db, today())
        await send_long(bot, chat_id_of(cb), text, reply_markup=markup)

    @router.callback_query(F.data.startswith("goaldel:"))
    async def cb_goaldel(cb: CallbackQuery, bot: Bot) -> None:
        g = goals.goal(db, int(cb.data.split(":")[1]))
        await cb.answer()
        if g and goals.drop_goal(db, g.id):
            await edit(cb, bot, f"Цель убрана: {html.escape(g.title)}.")
