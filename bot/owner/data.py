"""Names, import, export, backup and restore."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message

from diary import aliases, backup, program
from diary.db import Db
from diary.ingest import ImportReport, forget_post, import_export

from .. import pending
from ..common import find_exercise, kb, send_suggestion
from .ctx import Ctx, edit
from .restore import RestoreError, apply, pending_path, prepare, validate

log = logging.getLogger(__name__)
EQUIPMENT = {"штанга": "barbell", "гантели": "dumbbell", "тренажёр": "machine", "тренажер": "machine",
             "блок": "cable", "свой вес": "bodyweight"}
IMPORT_NAMES = re.compile(r"^(result\.json|messages\d*\.html)$")
MAX_UNPACKED = 50 * 1024 * 1024  # all export members together, uncompressed
BOT_API_LIMIT = 20 * 1024 * 1024  # Telegram gives bots files up to 20 MB
RESTORE_TTL = 3600


def extract_export(src: Path, folder: Path, name: str) -> int:
    """Put the export files from an upload into folder; a zip yields only result.json / messages*.html
    and may not unpack to more than MAX_UNPACKED. Returns how many files were taken."""
    if not name.endswith(".zip"):
        (folder / ("result.json" if name.endswith(".json") else "messages.html")).write_bytes(src.read_bytes())
        return 1
    with zipfile.ZipFile(src) as z:
        wanted = [i for i in z.infolist() if not i.is_dir() and IMPORT_NAMES.match(Path(i.filename).name)]
        if sum(i.file_size for i in wanted) > MAX_UNPACKED:
            raise ValueError("архив слишком большой после распаковки")
        for info in wanted:
            with z.open(info) as inp, (folder / Path(info.filename).name).open("wb") as out:
                shutil.copyfileobj(inp, out)  # never the archive's own paths
    return len(wanted)


def import_in_own_connection(db_path: Path, folder: Path) -> ImportReport:
    """Runs in a worker thread, so it opens its own connection."""
    own = Db(db_path)
    try:
        return import_export(own, folder)
    finally:
        own.conn.close()


def register(router: Router, ctx: Ctx) -> None:
    db = ctx.db

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
        if eid := await ctx.need_exercise(message, name, "/equip жим ногами = тренажёр"):
            db.set_equipment(eid, EQUIPMENT[kind])
            await message.answer(f"«{html.escape(db.exercise(eid)['name'])}»: {html.escape(kind)}. Шаг веса пересчитан.")

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
                             "(Telegram Desktop → канал → ⋮ → Экспорт истории, без медиа: боты получают файлы "
                             "до 20 МБ). Повторный импорт ничего не задваивает.")

    @router.message(Command("export"))
    async def on_export(message: Message) -> None:
        data = backup.export_csv(db)
        await message.answer_document(BufferedInputFile(data, filename=f"diary-{ctx.today()}.csv"),
                                      caption="Все подходы. Открывается в Excel и Google Таблицах.")

    @router.message(Command("backup"))
    async def on_backup(message: Message) -> None:
        path = backup.make_backup(db, ctx.today())
        await message.answer_document(FSInputFile(path), caption=BACKUP_CAPTION)

    # ---------- files ----------

    @router.message(F.document)
    async def on_document(message: Message, bot: Bot) -> None:
        doc = message.document
        name = (doc.file_name or "").lower()
        if not name.endswith((".json", ".html", ".zip", ".db")):
            await message.answer("Понимаю экспорт канала (result.json, messages.html или zip) "
                                 "и копию базы (.db) для восстановления.")
            return
        if doc.file_size and doc.file_size > BOT_API_LIMIT:
            await message.answer("Файл больше 20 МБ — Telegram не отдаёт такие боту. "
                                 "Для экспорта канала сними галочки с медиа.")
            return
        if name.endswith(".db"):
            await offer_restore(message, bot, doc)
        else:
            await import_file(message, bot, doc, name)

    async def import_file(message: Message, bot: Bot, doc, name: str) -> None:
        await message.answer("Читаю экспорт…")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "upload"
                await bot.download(doc, destination=src)
                folder = Path(tmp) / "export"
                folder.mkdir()
                try:
                    taken = extract_export(src, folder, name)
                except zipfile.BadZipFile:
                    await message.answer("Это не похоже на zip-архив.")
                    return
                except ValueError as e:
                    await message.answer(f"Не читаю: {html.escape(str(e))} (предел 50 МБ).")
                    return
                if not taken:
                    await message.answer("В архиве нет result.json или messages.html.")
                    return
                r = await asyncio.to_thread(import_in_own_connection, db.path, folder)
        except TelegramAPIError as e:
            await message.answer(f"Не получилось скачать файл: {html.escape(str(e)[:150])}")
            return
        await message.answer(
            f"Импорт: {r.posts} постов, {r.lines} строк с упражнениями ({r.parse_share:.0%} разобрано).\n"
            f"Новых упражнений: {r.new_exercises}, вопросов по названиям: {len(db.open_suggestions())} — /merge"
        )

    async def offer_restore(message: Message, bot: Bot, doc) -> None:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "upload.db"
                await bot.download(doc, destination=src)
                found = await asyncio.to_thread(validate, src, ctx.ident.owner_id)
                await asyncio.to_thread(prepare, src)
                shutil.move(str(src), pending_path(db))
        except RestoreError as e:
            await message.answer(html.escape(str(e)) + " Текущая база не тронута.")
            return
        except TelegramAPIError as e:
            await message.answer(f"Не получилось скачать файл: {html.escape(str(e)[:150])}")
            return
        pending.put(db, "restore", {"posts": found.posts, "sessions": found.sessions})
        await message.answer(f"В файле {found.posts} постов и {found.sessions} тренировок. "
                             "Восстановить из файла? Текущая база сначала уйдёт в бэкап.",
                             reply_markup=kb([("Восстановить", "restore:yes"), ("Нет", "restore:no")]))

    # ---------- buttons ----------

    @router.callback_query(F.data.startswith("restore:"))
    async def cb_restore(cb: CallbackQuery, bot: Bot) -> None:
        path, offered = pending_path(db), pending.get(db, "restore", RESTORE_TTL)
        if cb.data == "restore:no" or not offered or not path.exists():
            pending.drop(db, "restore")
            path.unlink(missing_ok=True)
            await edit(cb, bot, "Ок, ничего не меняю." if cb.data == "restore:no" else "Устарело — пришли файл снова.")
            await cb.answer()
            return
        try:
            got = apply(db, path)
        except Exception as e:  # noqa: BLE001 - the owner must hear that the restore did not happen
            log.exception("restore failed")
            await edit(cb, bot, f"Не вышло: {html.escape(type(e).__name__)}: {html.escape(str(e)[:200])}. "
                                "Копия текущей базы лежит в backups/.")
            await cb.answer()
            return
        pending.drop(db, "restore")
        path.unlink(missing_ok=True)
        await edit(cb, bot, f"Восстановил: {got.posts} постов, {got.sessions} тренировок. "
                            "Прежняя база — в backups/pre-restore-*.db.")
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
        await edit(cb, bot, done)
        await cb.answer()
        await send_suggestion(bot, db, cb.message.chat.id if cb.message else cb.from_user.id)


BACKUP_CAPTION = "Копия базы. Чтобы восстановить — просто пришли этот файл мне."
