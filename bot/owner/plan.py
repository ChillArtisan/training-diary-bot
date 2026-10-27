"""Every day: plans, keyboard buttons, cycle, program, pause and avoided exercises."""
from __future__ import annotations

import html
import re
from datetime import date, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from diary import clock, program, state
from diary.today import build_plan, render_html, swap_text

from .. import ui
from ..common import kb, post_plan, send_long
from .ctx import HELP, Ctx, chat_id_of, edit, reply


def parse_until(arg: str, today: date) -> date | None | str:
    """«7» -> today+7, «15.11» -> that date, «» -> None (until /resume), anything else -> 'bad'."""
    arg = arg.strip()
    if not arg:
        return None
    if arg.isdigit():
        return today + timedelta(days=int(arg))
    if m := re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", arg):
        y = int(m[3]) + (2000 if m[3] and len(m[3]) == 2 else 0) if m[3] else today.year
        try:
            d = date(y, int(m[2]), int(m[1]))
        except ValueError:
            return "bad"
        if not m[3] and d < today:
            d = d.replace(year=d.year + 1)
        return d
    return "bad"


def register(router: Router, ctx: Ctx) -> None:
    db, ident, today = ctx.db, ctx.ident, ctx.today

    @router.message(Command("start", "help"))
    async def on_help(message: Message) -> None:
        await message.answer(HELP, reply_markup=ui.MAIN)

    # ---------- keyboard ----------

    @router.message(F.text == ui.BTN_PLAN)
    async def kb_plan(message: Message, bot: Bot) -> None:
        await show_plan(bot, message.chat.id, "normal")

    @router.message(F.text == ui.BTN_LIGHT)
    async def kb_light(message: Message, bot: Bot) -> None:
        await show_plan(bot, message.chat.id, "light")

    @router.message(F.text == ui.BTN_SHORT)
    async def kb_short(message: Message, bot: Bot) -> None:
        await show_plan(bot, message.chat.id, "short")

    @router.message(F.text == ui.BTN_SWAP)
    async def kb_swap(message: Message, bot: Bot) -> None:
        await swap_menu(message.chat.id, bot)

    @router.message(F.text == ui.BTN_PROGRESS)
    async def kb_progress(message: Message) -> None:
        await message.answer("График чего показать?", reply_markup=ui.progress_menu(db, today()))

    @router.message(F.text == ui.BTN_ANALYTICS)
    async def kb_analytics(message: Message, bot: Bot) -> None:
        await ui.send_analytics(bot, message.chat.id, db, today())

    @router.message(F.text == ui.BTN_PAUSE)
    async def kb_pause(message: Message) -> None:
        if state.is_paused(db, today()):
            until = state.paused_until(db)
            when = "до /resume" if until and until.year == 9999 else f"до {until:%d.%m}"
            await message.answer(f"Сейчас пауза {when}.", reply_markup=kb([("▶️ Снять паузу", "resume")]))
            return
        await message.answer("Болеешь или уехал? Напоминания замолчат, запись тренировки снимет паузу сама.",
                             reply_markup=kb([("3 дня", "pause:3"), ("Неделя", "pause:7"), ("2 недели", "pause:14")],
                                             [("Пока не сниму", "pause:0")]))

    @router.message(F.text == ui.BTN_MORE)
    async def kb_more(message: Message) -> None:
        await message.answer(HELP, reply_markup=ui.MAIN)

    async def show_plan(bot: Bot, chat_id: int, mode: str) -> None:
        day = today()
        plan = build_plan(db, day, mode=mode)
        if plan is None:
            await bot.send_message(chat_id, "Нет активной программы. Выложи программу постом в канал или выбери в /program.")
            return
        state.note_plan(db, day, plan)
        buttons = [("В канал", f"post:{mode}")]
        buttons += [("Лёгкий", "plan:light"), ("Коротко", "plan:short")] if mode == "normal" else [("Обычный", "plan:normal")]
        await send_long(bot, chat_id, render_html(plan), reply_markup=kb(buttons, [("Замена", "swapmenu")]))

    @router.message(Command("plan"))
    async def on_plan(message: Message, bot: Bot) -> None:
        await show_plan(bot, message.chat.id, "normal")

    @router.message(Command("light"))
    async def on_light(message: Message, bot: Bot) -> None:
        await show_plan(bot, message.chat.id, "light")

    @router.message(Command("short"))
    async def on_short(message: Message, bot: Bot) -> None:
        await show_plan(bot, message.chat.id, "short")

    @router.message(Command("today"))
    async def on_today(message: Message, bot: Bot) -> None:
        if not ident.channel_id:
            await message.answer("Не знаю канал. Пришли /rebind или перешли сюда пост из канала-дневника.")
        elif await post_plan(bot, db, ident):
            await message.answer("Отправил в канал.")
        else:
            await message.answer("Нет активной программы — выбери в /program.")

    async def swap_menu(chat_id: int, bot: Bot) -> None:
        day = program.next_day(db)
        if not day:
            await bot.send_message(chat_id, "Нет активной программы.")
            return
        rows = [[(it["name"][:40], f"swap:{it['exercise_id']}")] for it in db.day_items(day["id"])]
        await bot.send_message(chat_id, "Что занято или не получается?", reply_markup=kb(*rows))

    @router.message(Command("swap"))
    async def on_swap(message: Message, command: CommandObject, bot: Bot) -> None:
        if not command.args:
            await swap_menu(message.chat.id, bot)
            return
        if eid := await ctx.need_exercise(message, command.args, "/swap жим лёжа"):
            await send_long(bot, message.chat.id, html.escape(swap_text(db, eid, today())))

    # ---------- cycle ----------

    @router.message(Command("cycle"))
    async def on_cycle(message: Message, command: CommandObject, bot: Bot) -> None:
        prog = db.active_program()
        days = db.program_days(prog["id"]) if prog else []
        if not days:
            await message.answer("Нет активной программы — выбери в /program.")
            return
        if command.args and command.args.strip().isdigit():
            n = int(command.args.strip())
            if not 1 <= n <= len(days):
                await message.answer(f"Дни от 1 до {len(days)}.")
                return
            program.set_next(db, n - 1)
        nxt = program.next_day(db)
        rows = [f"{'→' if d['id'] == nxt['id'] else '  '} {d['ord'] + 1}. неделя {d['week']} · {html.escape(d['title'])}"
                for d in days]
        await send_long(bot, message.chat.id, f"<b>{html.escape(prog['name'])}</b>\n" + "\n".join(rows))

    @router.message(Command("skip"))
    async def on_skip(message: Message) -> None:
        nxt = program.skip(db)
        await message.answer(f"Следующий: {html.escape(nxt['title'])}" if nxt else "Нет активной программы.")

    @router.message(Command("restart"))
    async def on_restart(message: Message) -> None:
        nxt = program.restart(db)
        await message.answer(f"Цикл с начала. Следующий: {html.escape(nxt['title'])}" if nxt else "Нет активной программы.")

    @router.message(Command("program"))
    async def on_program(message: Message) -> None:
        progs = db.programs(10)
        if not progs:
            await message.answer("Программ нет. Выложи программу постом в канал — пойму сам.")
            return
        rows = [[(f"{'✓ ' if p['active'] else ''}{p['day']} · {p['name'][:40]}", f"prog:{p['id']}")] for p in progs]
        await message.answer("Какая программа активна:", reply_markup=kb(*rows))

    # ---------- life ----------

    @router.message(Command("pause"))
    async def on_pause(message: Message, command: CommandObject) -> None:
        until = parse_until(command.args or "", today())
        if until == "bad":
            await message.answer("Формат: /pause, /pause 7 (дней) или /pause 15.11")
            return
        state.pause(db, today(), until)
        when = f"до {until:%d.%m}" if until else "до /resume"
        await message.answer(f"Пауза {when}. Напоминания молчат. Запишешь тренировку — пауза снимется сама.")

    @router.message(Command("resume"))
    async def on_resume(message: Message) -> None:
        state.resume(db, clock.today())
        plan = build_plan(db, today())
        extra = ""
        if plan and plan.gap_days and plan.gap_days > 21:
            extra = "\nПерерыв большой — можно начать цикл с начала: /restart"
        await message.answer("Пауза снята. Программа — /plan." + extra)

    @router.message(Command("avoid"))
    async def on_avoid(message: Message, command: CommandObject) -> None:
        if not command.args:
            names = [html.escape(db.exercise(e)["name"]) for e in state.avoided(db) if db.exercise(e)]
            await message.answer("Убрано из плана: " + ", ".join(names) if names
                                 else "Список пуст. Пример: /avoid жим лёжа")
            return
        if eid := await ctx.need_exercise(message, command.args, "/avoid жим лёжа"):
            state.avoid(db, eid)
            await message.answer(f"«{html.escape(db.exercise(eid)['name'])}» убрано из плана, ставлю замену. "
                                 f"Вернуть — /allow {html.escape(command.args.strip())}")

    @router.message(Command("allow"))
    async def on_allow(message: Message, command: CommandObject) -> None:
        if eid := await ctx.need_exercise(message, command.args, "/allow жим лёжа"):
            state.allow(db, eid)
            await message.answer(f"«{html.escape(db.exercise(eid)['name'])}» снова в плане.")

    # ---------- buttons ----------

    @router.callback_query(F.data.startswith("pause:"))
    async def cb_pause(cb: CallbackQuery, bot: Bot) -> None:
        days = int(cb.data.split(":")[1])
        until = today() + timedelta(days=days) if days else None
        state.pause(db, today(), until)
        await edit(cb, bot, f"Пауза {'до ' + format(until, '%d.%m') if until else 'до /resume'}. "
                            "Напоминания молчат; запишешь тренировку — пауза снимется сама.")
        await cb.answer()

    @router.callback_query(F.data == "resume")
    async def cb_resume(cb: CallbackQuery, bot: Bot) -> None:
        state.resume(db, clock.today())
        await edit(cb, bot, "Пауза снята. Программа — 📋 План.")
        await cb.answer()

    @router.callback_query(F.data.startswith("plan:"))
    async def cb_plan(cb: CallbackQuery, bot: Bot) -> None:
        await cb.answer()
        await show_plan(bot, chat_id_of(cb), cb.data.split(":")[1])

    @router.callback_query(F.data.startswith("post:"))
    async def cb_post(cb: CallbackQuery, bot: Bot) -> None:
        ok = await post_plan(bot, db, ident, mode=cb.data.split(":")[1])
        await cb.answer("Отправил в канал." if ok else "Не получилось: нет канала или программы.", show_alert=not ok)

    @router.callback_query(F.data == "swapmenu")
    async def cb_swapmenu(cb: CallbackQuery, bot: Bot) -> None:
        await cb.answer()
        await swap_menu(chat_id_of(cb), bot)

    @router.callback_query(F.data.startswith("swap:"))
    async def cb_swap(cb: CallbackQuery, bot: Bot) -> None:
        await cb.answer()
        await reply(cb, bot, html.escape(swap_text(db, int(cb.data.split(":")[1]), today())))

    @router.callback_query(F.data == "skipday")
    async def cb_skipday(cb: CallbackQuery, bot: Bot) -> None:
        nxt = program.skip(db)
        await edit(cb, bot, f"Пропустил. Следующий: {html.escape(nxt['title'])}" if nxt else "Нет программы.")
        await cb.answer()

    @router.callback_query(F.data == "later")
    async def cb_later(cb: CallbackQuery, bot: Bot) -> None:
        await edit(cb, bot, "Ок, этот день подождёт до следующей тренировки.")
        await cb.answer()

    @router.callback_query(F.data == "restart")
    async def cb_restart(cb: CallbackQuery, bot: Bot) -> None:
        nxt = program.restart(db)
        await edit(cb, bot, f"Цикл с начала. Следующий: {html.escape(nxt['title'])}" if nxt else "Нет программы.")
        await cb.answer()

    @router.callback_query(F.data.startswith("prog:"))
    async def cb_program(cb: CallbackQuery, bot: Bot) -> None:
        prog = db.program(int(cb.data.split(":")[1]))
        if not prog:
            await cb.answer("Программа удалена.")
            return
        program.activate(db, prog["id"])
        nxt = program.next_day(db)
        await edit(cb, bot, f"Активна: {html.escape(prog['name'])}\n"
                            f"Следующий день: {html.escape(nxt['title']) if nxt else '—'}")
        await cb.answer()
