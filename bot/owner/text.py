"""A workout typed in the private chat: confirm, post to the channel, ingest."""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, ReactionTypeEmoji

from diary.ingest import ingest_post
from diary.parse import parse_post

from .. import pending
from ..channel import reaction_for, report
from ..common import kb
from .ctx import Ctx, edit

log = logging.getLogger(__name__)


def register(router: Router, ctx: Ctx) -> None:
    db, ident = ctx.db, ctx.ident

    @router.message(F.text & ~F.text.startswith("/"))
    async def on_text(message: Message) -> None:
        parsed = parse_post(message.text)
        if parsed.kind == "note":
            await message.answer("Не похоже на тренировку. Команды — /help.")
            return
        if not ident.channel_id:
            await message.answer("Сначала подключи канал: перешли мне сюда любой пост из канала-дневника.")
            return
        pending.put(db, str(message.message_id), message.text)
        what = {"template": "программу", "body": "замер"}.get(parsed.kind, "тренировку")
        n = len(parsed.ex_lines)
        await message.answer(f"Записать {what} в канал?" + (f" Упражнений: {n}." if n else ""),
                             reply_markup=kb([("Записать", f"dmlog:{message.message_id}"),
                                              ("Нет", f"dmno:{message.message_id}")]))

    @router.message()
    async def on_other(message: Message) -> None:
        await message.answer("Не понял. Команды — /help.")

    @router.callback_query(F.data.startswith("dmlog:") | F.data.startswith("dmno:"))
    async def cb_dmlog(cb: CallbackQuery, bot: Bot) -> None:
        action, mid = cb.data.split(":")
        text = pending.get(db, mid)
        if action == "dmno" or not text:
            pending.drop(db, mid)
            await edit(cb, bot, "Ок, не записываю." if action == "dmno" else "Устарело.")
            await cb.answer()
            return
        if not ident.channel_id:
            await cb.answer("Канал не подключён. Текст сохранён.", show_alert=True)
            return
        try:
            sent = await bot.send_message(ident.channel_id, text, parse_mode=None)
        except TelegramAPIError as e:
            log.warning("posting a typed workout failed: %s", e)
            await cb.answer("Не получилось отправить в канал. Текст сохранён — нажми «Записать» ещё раз.",
                            show_alert=True)
            return
        pending.drop(db, mid)  # only now: the text is in the channel
        res = ingest_post(db, sent.message_id, sent.date, text)
        if emoji := reaction_for(res):
            try:
                await bot.set_message_reaction(ident.channel_id, sent.message_id, [ReactionTypeEmoji(emoji=emoji)])
            except TelegramAPIError:
                pass  # a reaction is cosmetic
        await edit(cb, bot, f'Записал в <a href="{html.escape(ident.post_link(sent.message_id))}">канал</a>.')
        await cb.answer()
        await report(bot, db, ident, res)
