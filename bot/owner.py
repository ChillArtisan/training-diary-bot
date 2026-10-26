"""Everything the owner does in the private chat: commands, buttons, logs and files."""
from __future__ import annotations

import html
import re
import tempfile
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, Message, MessageOriginChannel,
                           ReactionTypeEmoji)

from diary import aliases, backup, plates, program, state, stats
from diary.db import Db
from diary.ingest import forget_post, import_export, ingest_post
from diary.parse import parse_post
from diary.recommend import fmt_num
from diary.today import build_plan, last_sessions_text, render_html, swap_text

from . import prefs, ui
from .channel import reaction_for, report
from .common import Identity, find_exercise, kb, now_msk, post_plan, send_long, send_suggestion

STARTED = time.monotonic()
EQUIPMENT = {"штанга": "barbell", "гантели": "dumbbell", "тренажёр": "machine", "тренажер": "machine",
             "блок": "cable", "свой вес": "bodyweight"}
IMPORT_NAMES = re.compile(r"^(result\.json|messages\d*\.html)$")

HELP = """<b>Каждый день</b>
/plan — программа на сегодня (сюда)
/today — то же, но в канал
/light — плохой день: −15% и на подход меньше
/short — мало времени: только главное
/swap — тренажёр занят: чем заменить

<b>Цикл</b>
/cycle — где я; /cycle 6 — следующим будет день 6
/skip — пропустить день · /restart — цикл с начала
/program — сменить программу

<b>Жизнь</b>
/pause 7 — болею или уехал (или /pause 15.11) · /resume
/avoid жим лёжа — болит, убрать из плана · /allow — вернуть

<b>Прогресс и аналитика</b>
/analytics — постоянство, недели, объём по мышцам, выводы
/progress — график силы (кнопками или /progress жим лёжа)
/kpd — КПД тренировок · /body — вес и замеры
/ex жим лёжа — последние подходы · /stats — общая картина · /week — итоги недели

<b>Зал</b>
/plates 82.5 — блины на сторону · /warmup 100 — разминка

<b>Порядок</b>
/merge — похожие названия · /alias икры стоя = Подъём на носки стоя
/equip жим ногами = тренажёр · /forget &lt;ссылка&gt; — забыть пост
/import — пришли файл экспорта · /export — CSV · /backup — копия базы
/settings · /set ключ значение · /status

Тренировку можно прислать и сюда — я выложу её в канал. Главное — на кнопках снизу."""


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


def build_owner_router(db: Db, ident: Identity) -> Router:
    router = Router(name="owner")

    def is_owner(event) -> bool:
        user = event.from_user
        return user is not None and ident.owner_id is not None and user.id == ident.owner_id

    router.message.filter(F.chat.type == "private", is_owner)
    router.callback_query.filter(is_owner)

    def today() -> date:
        return now_msk().date()

    async def need_exercise(message: Message, query: str | None, example: str) -> int | None:
        if not query or not query.strip():
            await message.answer(f"Например: {example}")
            return None
        eid = find_exercise(db, query)
        if eid is None:
            await message.answer(f"Не нашёл «{html.escape(query.strip())}». Как оно записано в канале?")
        return eid

    # ---------- every day ----------

    @router.message(Command("start", "help"))
    async def on_help(message: Message) -> None:
        await message.answer(HELP, reply_markup=ui.MAIN)

    # ---------- keyboard ----------

    @router.message(F.text == ui.BTN_PLAN)
    async def kb_plan(message: Message) -> None:
        await show_plan(message, "normal")

    @router.message(F.text == ui.BTN_LIGHT)
    async def kb_light(message: Message) -> None:
        await show_plan(message, "light")

    @router.message(F.text == ui.BTN_SHORT)
    async def kb_short(message: Message) -> None:
        await show_plan(message, "short")

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

    async def show_plan(message: Message, mode: str) -> None:
        plan = build_plan(db, today(), mode=mode)
        if plan is None:
            await message.answer("Нет активной программы. Выложи программу постом в канал или выбери в /program.")
            return
        buttons = [("В канал", f"post:{mode}")]
        buttons += [("Лёгкий", "plan:light"), ("Коротко", "plan:short")] if mode == "normal" else [("Обычный", "plan:normal")]
        await send_long(message.bot, message.chat.id, render_html(plan), reply_markup=kb(buttons, [("Замена", "swapmenu")]))

    @router.message(Command("plan"))
    async def on_plan(message: Message) -> None:
        await show_plan(message, "normal")

    @router.message(Command("light"))
    async def on_light(message: Message) -> None:
        await show_plan(message, "light")

    @router.message(Command("short"))
    async def on_short(message: Message) -> None:
        await show_plan(message, "short")

    @router.message(Command("today"))
    async def on_today(message: Message, bot: Bot) -> None:
        if not ident.channel_id:
            await message.answer("Не знаю канал. Добавь меня админом в канал-дневник.")
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
        if eid := await need_exercise(message, command.args, "/swap жим лёжа"):
            await message.answer(html.escape(swap_text(db, eid, today())))

    # ---------- cycle ----------

    @router.message(Command("cycle"))
    async def on_cycle(message: Message, command: CommandObject) -> None:
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
        await send_long(message.bot, message.chat.id, f"<b>{html.escape(prog['name'])}</b>\n" + "\n".join(rows))

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
        state.resume(db)
        plan = build_plan(db, today())
        extra = ""
        if plan and plan.gap_days and plan.gap_days > 21:
            extra = "\nПерерыв большой — можно начать цикл с начала: /restart"
        await message.answer("Пауза снята. Программа — /plan." + extra)

    @router.message(Command("avoid"))
    async def on_avoid(message: Message, command: CommandObject) -> None:
        if not command.args:
            names = [db.exercise(e)["name"] for e in state.avoided(db) if db.exercise(e)]
            await message.answer("Убрано из плана: " + ", ".join(names) if names
                                 else "Список пуст. Пример: /avoid жим лёжа")
            return
        if eid := await need_exercise(message, command.args, "/avoid жим лёжа"):
            state.avoid(db, eid)
            await message.answer(f"«{html.escape(db.exercise(eid)['name'])}» убрано из плана, ставлю замену. "
                                 f"Вернуть — /allow {html.escape(command.args.strip())}")

    @router.message(Command("allow"))
    async def on_allow(message: Message, command: CommandObject) -> None:
        if eid := await need_exercise(message, command.args, "/allow жим лёжа"):
            state.allow(db, eid)
            await message.answer(f"«{html.escape(db.exercise(eid)['name'])}» снова в плане.")

    # ---------- progress ----------

    @router.message(Command("ex"))
    async def on_ex(message: Message, command: CommandObject) -> None:
        if eid := await need_exercise(message, command.args, "/ex жим лёжа"):
            await message.answer(f"<b>{html.escape(db.exercise(eid)['name'])}</b>\n"
                                 + html.escape(last_sessions_text(db, eid)))

    @router.message(Command("progress"))
    async def on_progress(message: Message, command: CommandObject, bot: Bot) -> None:
        if not command.args:
            await message.answer("График чего показать?", reply_markup=ui.progress_menu(db, today()))
            return
        if eid := await need_exercise(message, command.args, "/progress присед"):
            await ui.send_strength(bot, message.chat.id, db, eid)

    @router.message(Command("analytics"))
    async def on_analytics(message: Message, bot: Bot) -> None:
        await ui.send_analytics(bot, message.chat.id, db, today())

    @router.message(Command("kpd"))
    async def on_kpd(message: Message, bot: Bot) -> None:
        await ui.send_kpd(bot, message.chat.id, db)

    @router.message(Command("stats"))
    async def on_stats(message: Message) -> None:
        await send_long(message.bot, message.chat.id, stats.overall(db, today()))  # already HTML, names escaped

    @router.message(Command("week"))
    async def on_week(message: Message) -> None:
        await message.answer(html.escape(stats.weekly_summary(db, today())))

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
        await message.answer(plates.plates_text(total, b))

    @router.message(Command("warmup"))
    async def on_warmup(message: Message, command: CommandObject) -> None:
        nums = re.findall(r"\d+(?:[.,]\d+)?", command.args or "")
        if not nums:
            await message.answer("Например: /warmup 100")
            return
        work = float(nums[0].replace(",", "."))
        await message.answer(f"Разминка к {fmt_num(work)} кг: {plates.warmup_text(work, bar())}")

    # ---------- housekeeping ----------

    @router.message(Command("alias"))
    async def on_alias(message: Message, command: CommandObject) -> None:
        if not command.args or "=" not in command.args:
            await message.answer("Формат: /alias икры стоя = Подъём на носки стоя")
            return
        text, name = (s.strip() for s in command.args.split("=", 1))
        target = db.exercise_by_name(name)
        eid = target["id"] if target else find_exercise(db, name)
        if eid is None:
            await message.answer(f"Нет упражнения «{html.escape(name)}».")
            return
        src = db.exercise_by_alias(aliases.norm(text))
        if src is not None and src != eid:
            db.merge_exercises(src, eid)
        else:
            with db.tx():
                db.add_alias(aliases.norm(text), eid)
        program.assign_positions(db)
        await message.answer(f"«{html.escape(text)}» = «{html.escape(db.exercise(eid)['name'])}».")

    @router.message(Command("merge"))
    async def on_merge(message: Message, bot: Bot) -> None:
        if not await send_suggestion(bot, db, message.chat.id):
            await message.answer("Вопросов по названиям нет.")

    @router.message(Command("equip"))
    async def on_equip(message: Message, command: CommandObject) -> None:
        if not command.args or "=" not in command.args:
            await message.answer("Формат: /equip жим ногами = тренажёр\nВарианты: " + ", ".join(EQUIPMENT))
            return
        name, kind = (s.strip().lower() for s in command.args.split("=", 1))
        if kind not in EQUIPMENT:
            await message.answer("Варианты: " + ", ".join(EQUIPMENT))
            return
        if eid := await need_exercise(message, name, "/equip жим ногами = тренажёр"):
            db.set_equipment(eid, EQUIPMENT[kind])
            await message.answer(f"«{html.escape(db.exercise(eid)['name'])}»: {kind}. Шаг веса пересчитан.")

    @router.message(Command("forget"))
    async def on_forget(message: Message, command: CommandObject) -> None:
        m = re.search(r"(\d+)\s*$", command.args or "")
        if not m:
            await message.answer("Формат: /forget https://t.me/c/…/123")
            return
        await message.answer("Забыл." if forget_post(db, int(m.group(1))) else "Такого поста нет в базе.")

    @router.message(Command("import"))
    async def on_import_help(message: Message) -> None:
        await message.answer("Пришли файл экспорта канала: result.json, messages.html или zip с ними "
                             "(Telegram Desktop → канал → ⋮ → Экспорт истории). Повторный импорт ничего не задваивает.")

    @router.message(Command("export"))
    async def on_export(message: Message) -> None:
        data = backup.export_csv(db)
        await message.answer_document(BufferedInputFile(data, filename=f"diary-{today()}.csv"),
                                      caption="Все подходы. Открывается в Excel и Google Таблицах.")

    @router.message(Command("backup"))
    async def on_backup(message: Message) -> None:
        path = backup.make_backup(db, today())
        await message.answer_document(FSInputFile(path), caption="Копия базы. Восстановление — положить файл "
                                                                 "вместо data/diary.db.")

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
            f"Версия {ident.cfg.version}, работает {uptime}",
            f"Канал: {channel}",
            f"Постов: {sum(db.post_counts().values())}, тренировок: {c['sessions']}, подходов: {c['sets']}",
            f"Последний пост: {last['day'] if last else '—'}",
            f"Программа: {html.escape(prog['name']) if prog else '—'}; следующий: {html.escape(nxt['title']) if nxt else '—'}",
            f"Пауза до {until:%d.%m.%Y}" if until and state.is_paused(db, today()) else "Пауза: нет",
            f"Бэкап: {db.get('last_backup') or 'ещё не было'}; база {size:.0f} КБ",
            f"Вопросов по названиям: {len(db.open_suggestions())}",
        ]
        await message.answer("\n".join(rows))

    # ---------- forwarded posts, files, free text ----------

    @router.message(F.forward_origin.as_("origin"))
    async def on_forward(message: Message, origin) -> None:
        if not isinstance(origin, MessageOriginChannel) or origin.chat.id != ident.channel_id:
            await message.answer("Пересылай посты из дневника — тренировку можно просто написать сюда.")
            return
        prog = db.program_by_post(origin.message_id)
        if not prog:
            await message.answer("Этот пост не похож на программу.")
            return
        await message.answer(f"Программа «{html.escape(prog['name'])}».",
                             reply_markup=kb([("Сделать активной", f"prog:{prog['id']}")]))

    @router.message(F.document)
    async def on_document(message: Message, bot: Bot) -> None:
        doc = message.document
        name = (doc.file_name or "").lower()
        if not name.endswith((".json", ".html", ".zip")):
            await message.answer("Понимаю только экспорт канала: result.json, messages.html или zip.")
            return
        await message.answer("Читаю экспорт…")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "upload"
            await bot.download(doc, destination=src)
            folder = Path(tmp) / "export"
            folder.mkdir()
            if name.endswith(".zip"):
                with zipfile.ZipFile(src) as z:
                    for info in z.infolist():
                        base = Path(info.filename).name
                        if IMPORT_NAMES.match(base):  # only the files we need, never the archive's paths
                            (folder / base).write_bytes(z.read(info))
            else:
                (folder / ("result.json" if name.endswith(".json") else "messages.html")).write_bytes(src.read_bytes())
            if not any(folder.iterdir()):
                await message.answer("В архиве нет result.json или messages.html.")
                return
            r = import_export(db, folder)
        await message.answer(
            f"Импорт: {r.posts} постов, {r.lines} строк с упражнениями ({r.parse_share:.0%} разобрано).\n"
            f"Новых упражнений: {r.new_exercises}, вопросов по названиям: {len(db.open_suggestions())} — /merge"
        )

    @router.message(F.text & ~F.text.startswith("/"))
    async def on_text(message: Message) -> None:
        parsed = parse_post(message.text)
        if parsed.kind == "note":
            await message.answer("Не похоже на тренировку. Команды — /help.")
            return
        if not ident.channel_id:
            await message.answer("Сначала добавь меня админом в канал-дневник.")
            return
        db.set_json(f"pending:{message.message_id}", message.text)
        what = {"template": "программу", "body": "замер"}.get(parsed.kind, "тренировку")
        n = len(parsed.ex_lines)
        await message.answer(f"Записать {what} в канал?" + (f" Упражнений: {n}." if n else ""),
                             reply_markup=kb([("Записать", f"dmlog:{message.message_id}"),
                                              ("Нет", f"dmno:{message.message_id}")]))

    @router.message()
    async def on_other(message: Message) -> None:
        await message.answer("Не понял. Команды — /help.")

    # ---------- buttons ----------

    @router.callback_query(F.data.startswith("dmlog:") | F.data.startswith("dmno:"))
    async def cb_dmlog(cb: CallbackQuery, bot: Bot) -> None:
        action, mid = cb.data.split(":")
        key = f"pending:{mid}"
        text = db.get_json(key)
        db.unset(key)
        if action == "dmno" or not text:
            await cb.message.edit_text("Ок, не записываю." if action == "dmno" else "Устарело.")
            await cb.answer()
            return
        sent = await bot.send_message(ident.channel_id, text, parse_mode=None)
        res = ingest_post(db, sent.message_id, sent.date, text)
        if emoji := reaction_for(res):
            try:
                await bot.set_message_reaction(ident.channel_id, sent.message_id, [ReactionTypeEmoji(emoji=emoji)])
            except TelegramAPIError:
                pass  # a reaction is cosmetic
        await cb.message.edit_text(f'Записал в <a href="{ident.post_link(sent.message_id)}">канал</a>.')
        await cb.answer()
        await report(bot, db, ident, res)

    @router.callback_query(F.data == "progressmenu")
    async def cb_progressmenu(cb: CallbackQuery) -> None:
        await cb.answer()
        await cb.message.answer("График чего показать?", reply_markup=ui.progress_menu(db, today()))

    @router.callback_query(F.data.startswith("chart:"))
    async def cb_chart(cb: CallbackQuery, bot: Bot) -> None:
        await cb.answer("Рисую…")
        what = cb.data.split(":")[1]
        if what == "body":
            await ui.send_body(bot, cb.message.chat.id, db)
        elif what == "kpd":
            await ui.send_kpd(bot, cb.message.chat.id, db)
        elif db.exercise(int(what)):
            await ui.send_strength(bot, cb.message.chat.id, db, int(what))

    @router.callback_query(F.data.startswith("pause:"))
    async def cb_pause(cb: CallbackQuery) -> None:
        days = int(cb.data.split(":")[1])
        until = today() + timedelta(days=days) if days else None
        state.pause(db, today(), until)
        await cb.message.edit_text(f"Пауза {'до ' + format(until, '%d.%m') if until else 'до /resume'}. "
                                   "Напоминания молчат; запишешь тренировку — пауза снимется сама.")
        await cb.answer()

    @router.callback_query(F.data == "resume")
    async def cb_resume(cb: CallbackQuery) -> None:
        state.resume(db)
        await cb.message.edit_text("Пауза снята. Программа — 📋 План.")
        await cb.answer()

    @router.callback_query(F.data.startswith("plan:"))
    async def cb_plan(cb: CallbackQuery) -> None:
        await cb.answer()
        await show_plan(cb.message, cb.data.split(":")[1])

    @router.callback_query(F.data.startswith("post:"))
    async def cb_post(cb: CallbackQuery, bot: Bot) -> None:
        ok = await post_plan(bot, db, ident, mode=cb.data.split(":")[1])
        await cb.answer("Отправил в канал." if ok else "Не получилось: нет канала или программы.", show_alert=not ok)

    @router.callback_query(F.data == "swapmenu")
    async def cb_swapmenu(cb: CallbackQuery, bot: Bot) -> None:
        await cb.answer()
        await swap_menu(cb.message.chat.id, bot)

    @router.callback_query(F.data.startswith("swap:"))
    async def cb_swap(cb: CallbackQuery) -> None:
        await cb.answer()
        await cb.message.answer(html.escape(swap_text(db, int(cb.data.split(":")[1]), today())))

    @router.callback_query(F.data == "skipday")
    async def cb_skipday(cb: CallbackQuery) -> None:
        nxt = program.skip(db)
        await cb.message.edit_text(f"Пропустил. Следующий: {html.escape(nxt['title'])}" if nxt else "Нет программы.")
        await cb.answer()

    @router.callback_query(F.data == "later")
    async def cb_later(cb: CallbackQuery) -> None:
        await cb.message.edit_text("Ок, этот день подождёт до следующей тренировки.")
        await cb.answer()

    @router.callback_query(F.data == "restart")
    async def cb_restart(cb: CallbackQuery) -> None:
        nxt = program.restart(db)
        await cb.message.edit_text(f"Цикл с начала. Следующий: {html.escape(nxt['title'])}" if nxt else "Нет программы.")
        await cb.answer()

    @router.callback_query(F.data.startswith("prog:"))
    async def cb_program(cb: CallbackQuery) -> None:
        prog = db.program(int(cb.data.split(":")[1]))
        if not prog:
            await cb.answer("Программа удалена.")
            return
        program.activate(db, prog["id"])
        nxt = program.next_day(db)
        await cb.message.edit_text(f"Активна: {html.escape(prog['name'])}\n"
                                   f"Следующий день: {html.escape(nxt['title']) if nxt else '—'}")
        await cb.answer()

    @router.callback_query(F.data.startswith("merge:") | F.data.startswith("keep:"))
    async def cb_suggestion(cb: CallbackQuery, bot: Bot) -> None:
        action, sid = cb.data.split(":")
        s = db.suggestion(int(sid))
        if not s or s["status"] != "open":
            await cb.answer("Уже решено.")
            return
        if action == "merge":
            db.merge_exercises(s["exercise_id"], s["candidate_id"])
            program.assign_positions(db)
            done = f"✓ «{html.escape(s['name'])}» = «{html.escape(s['candidate'])}»"
        else:
            db.set_suggestion(s["id"], "kept")
            done = f"✓ «{html.escape(s['name'])}» — отдельное упражнение"
        await cb.message.edit_text(done)
        await cb.answer()
        await send_suggestion(bot, db, cb.message.chat.id)

    return router


def build_claim_router(db: Db, ident: Identity) -> Router:
    """Before an owner is known: only a channel admin can claim the bot."""
    router = Router(name="claim")
    router.message.filter(F.chat.type == "private", lambda m: ident.owner_id is None)

    async def admin_of(bot: Bot, chat_id: int, user_id: int) -> bool:
        try:
            return (await bot.get_chat_member(chat_id, user_id)).status in ("creator", "administrator")
        except TelegramAPIError:
            return False

    @router.message(Command("start"))
    async def on_start(message: Message, bot: Bot) -> None:
        if not ident.channel_id:
            await message.answer("Привет! Я веду тренировочный дневник в твоём канале.\n"
                                 "1. Добавь меня админом в канал с правом публикации.\n"
                                 "2. Перешли мне сюда любой пост из этого канала.")
            return
        if not await admin_of(bot, ident.channel_id, message.from_user.id):
            await message.answer("Это личный бот дневника. Владелец — админ канала.")
            return
        ident.set_owner(message.from_user.id)
        await message.answer("Готово, ты владелец.\n\n" + HELP, reply_markup=ui.MAIN)

    @router.message(F.forward_origin.as_("origin"))
    async def on_forward(message: Message, origin, bot: Bot) -> None:
        """Pairing without config: a forwarded post names the channel, Telegram confirms both are admins."""
        if not isinstance(origin, MessageOriginChannel):
            await message.answer("Перешли пост именно из канала-дневника.")
            return
        chat_id = origin.chat.id
        if ident.channel_id and chat_id != ident.channel_id:
            await message.answer("Я уже работаю с другим каналом.")
            return
        if not await admin_of(bot, chat_id, bot.id):
            await message.answer("Я не админ в этом канале. Добавь меня админом с правом публикации и перешли пост снова.")
            return
        if not await admin_of(bot, chat_id, message.from_user.id):
            await message.answer("Это личный бот дневника. Владелец — админ канала.")
            return
        ident.set_channel(chat_id)
        ident.set_owner(message.from_user.id)
        await message.answer(f"Готово: канал «{html.escape(origin.chat.title or '')}», ты владелец.\n"
                             "Старые посты подтяну, если пришлёшь экспорт канала (/import).\n\n" + HELP,
                             reply_markup=ui.MAIN)

    return router
