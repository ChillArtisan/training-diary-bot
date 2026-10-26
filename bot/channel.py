"""The diary channel: posts, edits and the bot's own membership."""
from __future__ import annotations

import html
import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberUpdated, Message, ReactionTypeEmoji

from diary import analytics
from diary.db import Db
from diary.ingest import IngestResult, ingest_post

from . import prefs
from .common import Identity, dm, kb, send_suggestion

log = logging.getLogger(__name__)
LOST = ("left", "kicked", "member", "restricted")


def reaction_for(res: IngestResult) -> str | None:
    if res.kind == "template":
        return "✍"
    if res.kind == "body":
        return "👍"
    if res.kind != "log":
        return None
    if res.records:
        return "🔥"
    return "🤔" if res.unparsed else "👍"


async def report(bot: Bot, db: Db, ident: Identity, res: IngestResult, live: bool = True) -> None:
    """Tell the owner what needs attention after a post was stored."""
    link = ident.post_link(res.message_id)
    if res.unparsed:
        lines = "\n".join(f"• {html.escape(u)}" for u in res.unparsed)
        await dm(bot, ident, f'Не разобрал строки в <a href="{link}">посте</a>:\n{lines}\n'
                             f"Поправь пост — я перечитаю его сам.")
    if live and res.kind == "log" and res.sets:
        lines = []
        if prefs.on(db, "log_feedback") and (k := analytics.post_kpd(db, res.message_id)) and k.score is not None:
            lines.append(f"Записал{' «' + html.escape(k.title) + '»' if k.title else ''}. {html.escape(k.text())}")
        if res.records:
            lines.append("🔥 Рекорд!\n" + "\n".join(html.escape(r.text()) for r in res.records))
        if lines:
            await dm(bot, ident, "\n\n".join(lines))
    if res.resumed:
        await dm(bot, ident, "С возвращением! Пауза снята, напоминания снова включены.")
    if res.suggestions and ident.owner_id:  # one question at a time; each answer brings the next
        await send_suggestion(bot, db, ident.owner_id, res.suggestions[0])
    if live and res.kind == "template" and res.program_id:
        prog = db.program(res.program_id)
        if prog and not prog["active"]:
            await dm(bot, ident,
                     f'Похоже на новую программу: <a href="{link}">{html.escape(prog["name"])}</a>. Сделать активной?',
                     reply_markup=kb([("Сделать активной", f"prog:{prog['id']}")]))
        elif prog:
            await dm(bot, ident, f"Программа «{html.escape(prog['name'])}» активна. Первый день — /plan.")


def build_channel_router(db: Db, ident: Identity) -> Router:
    router = Router(name="channel")

    def ours(event) -> bool:
        chat = event.chat
        return chat.type == "channel" and (ident.channel_id is None or chat.id == ident.channel_id)

    router.channel_post.filter(ours)
    router.edited_channel_post.filter(ours)

    async def process(message: Message, bot: Bot, edited: bool) -> None:
        if ident.channel_id is None:
            ident.set_channel(message.chat.id)
            await dm(bot, ident, f"Подключился к каналу «{html.escape(message.chat.title or '')}».")
        if db.is_sent(message.message_id):
            return
        text = message.text or message.caption
        if not text:
            return
        res = ingest_post(db, message.message_id, message.date, text, message.edit_date if edited else None)
        if emoji := reaction_for(res):
            try:
                await bot.set_message_reaction(message.chat.id, message.message_id, [ReactionTypeEmoji(emoji=emoji)])
            except TelegramAPIError as e:
                log.warning("reaction failed: %s", e)
        await report(bot, db, ident, res, live=not edited)

    @router.channel_post()
    async def on_post(message: Message, bot: Bot) -> None:
        await process(message, bot, edited=False)

    @router.edited_channel_post()
    async def on_edit(message: Message, bot: Bot) -> None:
        await process(message, bot, edited=True)

    @router.my_chat_member()
    async def on_membership(update: ChatMemberUpdated, bot: Bot) -> None:
        chat, member = update.chat, update.new_chat_member
        if chat.type != "channel":
            return
        if member.status == "administrator":
            if ident.channel_id is None:
                ident.set_channel(chat.id)
            if chat.id != ident.channel_id:
                return
            if ident.owner_id is None and update.from_user and not update.from_user.is_bot:
                ident.set_owner(update.from_user.id)  # whoever made the bot an admin runs this channel
            note = "" if getattr(member, "can_post_messages", False) else \
                "\n⚠️ Нет права «Публикация сообщений» — не смогу присылать программу в канал."
            await dm(bot, ident, f"Подключился к каналу «{html.escape(chat.title or '')}». "
                                 f"Пиши тренировки как раньше, команды — /help.{note}")
        elif chat.id == ident.channel_id and member.status in LOST:
            await dm(bot, ident, f"⚠️ Меня убрали из админов «{html.escape(chat.title or '')}» — "
                                 "новые посты не вижу. Верни права админа, и всё продолжится.")

    return router
