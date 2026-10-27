"""Progress, analytics, charts and the gym helpers (plates, warm-up)."""
from __future__ import annotations

import html
import re

from aiogram import Bot, Router
from aiogram.filters import Command, CommandObject
from aiogram import F
from aiogram.types import CallbackQuery, Message

from diary import plates, stats
from diary.recommend import fmt_num
from diary.today import last_sessions_text

from .. import prefs, ui
from ..common import send_long
from .ctx import Ctx, chat_id_of, reply


def register(router: Router, ctx: Ctx) -> None:
    db, today = ctx.db, ctx.today

    @router.message(Command("ex"))
    async def on_ex(message: Message, command: CommandObject, bot: Bot) -> None:
        if eid := await ctx.need_exercise(message, command.args, "/ex жим лёжа"):
            await send_long(bot, message.chat.id, f"<b>{html.escape(db.exercise(eid)['name'])}</b>\n"
                            + html.escape(last_sessions_text(db, eid)))

    @router.message(Command("progress"))
    async def on_progress(message: Message, command: CommandObject, bot: Bot) -> None:
        if not command.args:
            await message.answer("График чего показать?", reply_markup=ui.progress_menu(db, today()))
            return
        if eid := await ctx.need_exercise(message, command.args, "/progress присед"):
            await ui.send_strength(bot, message.chat.id, db, eid)

    @router.message(Command("analytics"))
    async def on_analytics(message: Message, bot: Bot) -> None:
        await ui.send_analytics(bot, message.chat.id, db, today())

    @router.message(Command("kpd"))
    async def on_kpd(message: Message, bot: Bot) -> None:
        await ui.send_kpd(bot, message.chat.id, db)

    @router.message(Command("stats"))
    async def on_stats(message: Message, bot: Bot) -> None:
        await send_long(bot, message.chat.id, stats.overall(db, today()))  # already HTML, names escaped

    @router.message(Command("week"))
    async def on_week(message: Message, bot: Bot) -> None:
        await send_long(bot, message.chat.id, html.escape(stats.weekly_summary(db, today())))

    @router.message(Command("body"))
    async def on_body(message: Message, bot: Bot) -> None:
        await ui.send_body(bot, message.chat.id, db)

    # ---------- gym ----------

    def bar() -> float:
        return float(prefs.get(db, "bar_weight"))

    @router.message(Command("plates"))
    async def on_plates(message: Message, command: CommandObject) -> None:
        nums = re.findall(r"\d+(?:[.,]\d+)?", command.args or "")
        if not nums:
            await message.answer("Например: /plates 82.5 или /plates 60 15 (гриф 15)")
            return
        total = float(nums[0].replace(",", "."))
        b = float(nums[1].replace(",", ".")) if len(nums) > 1 else bar()
        await message.answer(html.escape(plates.plates_text(total, b)))

    @router.message(Command("warmup"))
    async def on_warmup(message: Message, command: CommandObject) -> None:
        nums = re.findall(r"\d+(?:[.,]\d+)?", command.args or "")
        if not nums:
            await message.answer("Например: /warmup 100")
            return
        work = float(nums[0].replace(",", "."))
        await message.answer(f"Разминка к {fmt_num(work)} кг: {html.escape(plates.warmup_text(work, bar()))}")

    # ---------- buttons ----------

    @router.callback_query(F.data == "progressmenu")
    async def cb_progressmenu(cb: CallbackQuery, bot: Bot) -> None:
        await cb.answer()
        await reply(cb, bot, "График чего показать?", reply_markup=ui.progress_menu(db, today()))

    @router.callback_query(F.data.startswith("chart:"))
    async def cb_chart(cb: CallbackQuery, bot: Bot) -> None:
        await cb.answer("Рисую…")
        what, chat = cb.data.split(":")[1], chat_id_of(cb)
        if what == "body":
            await ui.send_body(bot, chat, db)
        elif what == "kpd":
            await ui.send_kpd(bot, chat, db)
        elif db.exercise(int(what)):
            await ui.send_strength(bot, chat, db, int(what))
