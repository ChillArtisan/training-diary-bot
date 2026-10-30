"""Who the owner is and which channel is the diary: claiming, binding, rebinding, forwarded posts."""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, MessageOriginChannel, ReactionTypeEmoji

from diary.db import Db
from diary.ingest import ingest_post, untouched_plan

from .. import pending, ui
from ..channel import reaction_for, report
from ..common import Identity, kb
from .ctx import HELP, Ctx, edit

log = logging.getLogger(__name__)
REFUSAL = "Это личный бот дневника."
REBIND_TTL = 600


async def admin_of(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        return (await bot.get_chat_member(chat_id, user_id)).status in ("creator", "administrator")
    except TelegramAPIError:
        return False


async def admins_problem(bot: Bot, chat_id: int, user_id: int) -> str | None:
    """Why this channel cannot be bound (Telegram itself confirms both are admins), or None."""
    if not await admin_of(bot, chat_id, bot.id):
        return "Я не админ в этом канале. Добавь меня админом с правом публикации и перешли пост снова."
    if not await admin_of(bot, chat_id, user_id):
        return "Ты не админ этого канала."
    return None


def build_claim_router(db: Db, ident: Identity) -> Router:
    """Before an owner is known: only the pair code from the log makes someone the owner."""
    router = Router(name="claim")
    router.message.filter(F.chat.type == "private", lambda m: ident.owner_id is None)

    @router.message(Command("start"))
    async def on_start(message: Message, command: CommandObject) -> None:
        user = message.from_user
        if user and command.args and ident.try_claim(command.args, user.id):
            log.warning("owner claimed by user %s", user.id)
            step = "" if ident.channel_id else ("Теперь добавь меня админом в канал-дневник с правом публикации "
                                                "и перешли мне сюда любой пост из него.\n\n")
            await message.answer("Готово, ты владелец.\n" + step + HELP, reply_markup=ui.MAIN)
            return
        log.warning("refused /start from user %s", user.id if user else None)
        await message.answer(REFUSAL)

    @router.message()
    async def on_other(message: Message) -> None:
        await message.answer(REFUSAL)

    return router


def register(router: Router, ctx: Ctx) -> None:
    db, ident = ctx.db, ctx.ident

    @router.message(Command("rebind"))
    async def on_rebind(message: Message) -> None:
        if ident.cfg.channel_id:
            await message.answer("Канал задан в CHANNEL_ID (.env). Поменяй его там и перезапусти бота.")
            return
        pending.put(db, "rebind", True)
        await message.answer("Перешли пост из нового канала. Я и ты должны быть там админами. Жду 10 минут.")

    @router.message(F.forward_origin.as_("origin"))
    async def on_forward(message: Message, origin, bot: Bot) -> None:
        if not isinstance(origin, MessageOriginChannel):
            await message.answer("Пересылай посты из дневника — тренировку можно просто написать сюда.")
            return
        cid = origin.chat.id
        title = html.escape(origin.chat.title or "")
        if ident.channel_id is None:
            if problem := await admins_problem(bot, cid, message.from_user.id):
                await message.answer(problem)
                return
            ident.set_channel(cid)
            await message.answer(f"Готово: канал «{title}». Старые посты подтяну из экспорта (/import) "
                                 "или если перешлёшь их мне.")
        elif cid != ident.channel_id:
            if pending.get(db, "rebind", REBIND_TTL):
                if problem := await admins_problem(bot, cid, message.from_user.id):
                    await message.answer(problem)
                    return
                await message.answer(f"Перейти на канал «{title}»? Посты старого канала останутся в базе.",
                                     reply_markup=kb([("Да, сменить", f"rebind:{cid}"), ("Нет", "rebind:no")]))
            else:
                await message.answer("Я работаю с другим каналом. Сменить — /rebind.")
            return
        await ingest_forward(message, origin, bot)

    async def ingest_forward(message: Message, origin: MessageOriginChannel, bot: Bot) -> None:
        text = message.text or message.caption
        if not text:
            await message.answer("В этом посте нет текста.")
            return
        if untouched_plan(db, origin.message_id, text):
            await message.answer("Это программа бота без правок — записывать пока нечего.")
            return
        res = ingest_post(db, origin.message_id, origin.date, text)
        if emoji := reaction_for(res):
            try:
                await bot.set_message_reaction(origin.chat.id, origin.message_id, [ReactionTypeEmoji(emoji=emoji)])
            except TelegramAPIError as e:
                log.warning("reaction failed: %s", e)
        await message.answer("Пост учтён.")
        await report(bot, db, ident, res)

    @router.callback_query(F.data.startswith("rebind:"))
    async def cb_rebind(cb: CallbackQuery, bot: Bot) -> None:
        arg = cb.data.split(":", 1)[1]
        if arg == "no":
            pending.drop(db, "rebind")
            await edit(cb, bot, "Ок, остаюсь с прежним каналом.")
        elif not pending.get(db, "rebind", REBIND_TTL):
            await edit(cb, bot, "Устарело. Начни с /rebind.")
        elif problem := await admins_problem(bot, int(arg), cb.from_user.id):
            await edit(cb, bot, problem)
        else:
            ident.set_channel(int(arg))
            pending.drop(db, "rebind")
            await edit(cb, bot, "Готово, канал сменён. Старые посты нового канала — через экспорт (/import) "
                                "или пересылкой.")
        await cb.answer()
