"""The diary channel: posts, edits and the bot's own membership."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message, ReactionTypeEmoji

from diary import analytics, clock, goals, stats
from diary.db import Db
from diary.ingest import IngestResult, ingest_post, untouched_plan

from . import prefs
from .common import PLAN_DONE, Identity, dm, kb, plan_markup, send_suggestion

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
    if live and res.kind in ("log", "body"):
        for g in goals.check_achieved(db, clock.today()):
            await dm(bot, ident, f"🏁 Цель взята: {html.escape(g.title)}! Поставь следующую — /goal.")
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


async def react(bot: Bot, chat_id: int | None, message_id: int, emoji: str | None) -> None:
    if not chat_id or not emoji:
        return
    try:
        await bot.set_message_reaction(chat_id, message_id, [ReactionTypeEmoji(emoji=emoji)])
    except TelegramAPIError as e:
        log.warning("reaction failed: %s", e)


# A workout is posted at the start (a copy of the plan, or the bot's plan post itself) and edited set by set, so its report
# (KPD, records, unreadable lines, name questions) waits until the post has been quiet for `log_settle` minutes.
LIVE_KEY = "live_posts"
KEEP_DONE = timedelta(days=3)


def settle_minutes(db: Db) -> int:
    return int(prefs.get(db, "log_settle"))


def forget_workout(db: Db, message_id: int) -> None:
    posts = db.get_json(LIVE_KEY, {})
    if posts.pop(str(message_id), None) is not None:
        db.set_json(LIVE_KEY, posts)


async def track_workout(bot: Bot, db: Db, ident: Identity, res: IngestResult, finished: bool = False) -> None:
    """`finished`: the owner said the workout is over (the button under the plan post), so no waiting."""
    posts = db.get_json(LIVE_KEY, {})
    entry = posts.setdefault(str(res.message_id), {})
    entry["at"] = clock.now().isoformat()
    entry["unparsed"] = res.unparsed  # the latest text decides: a line fixed later is not reported
    entry["suggestions"] = sorted(set(entry.get("suggestions", [])) | set(res.suggestions))
    entry["resumed"] = bool(entry.get("resumed") or res.resumed)
    db.set_json(LIVE_KEY, posts)
    if entry.get("done"):
        # corrected after the report: refresh the reaction, mention only lines that were not mentioned yet
        res.records = stats.post_records(db, res.message_id)
        await react(bot, ident.channel_id, res.message_id, reaction_for(res))
        fresh = [u for u in res.unparsed if u not in entry.get("told", [])]
        if fresh:
            entry["told"] = entry.get("told", []) + fresh
            db.set_json(LIVE_KEY, posts)
            await report(bot, db, ident, IngestResult(res.message_id, "log", unparsed=fresh), live=False)
        return
    if finished or settle_minutes(db) == 0:
        await settle_workouts(bot, db, ident, only=res.message_id)
    else:
        await react(bot, ident.channel_id, res.message_id, "✍")  # in progress


async def settle_workouts(bot: Bot, db: Db, ident: Identity, now: datetime | None = None,
                          only: int | None = None) -> int:
    """Report the workouts nobody has edited for a while (called by the scheduler), or `only` this one at once."""
    now = now or clock.now()
    wait = timedelta(minutes=settle_minutes(db))
    posts = db.get_json(LIVE_KEY, {})
    done = 0
    for key, entry in list(posts.items()):
        if only is not None and key != str(only):
            continue
        quiet = now - datetime.fromisoformat(entry["at"])
        if entry.get("done"):
            if quiet > KEEP_DONE:
                del posts[key]
            continue
        if quiet < wait and only is None:
            continue
        mid = int(key)
        post = db.post(mid)
        if not post or post["kind"] != "log":
            del posts[key]
            continue
        open_ids = [s for s in entry.get("suggestions", []) if (row := db.suggestion(s)) and row["status"] == "open"]
        res = IngestResult(mid, "log", sets=len(db.post_sets(mid)), unparsed=entry.get("unparsed", []),
                           suggestions=open_ids, records=stats.post_records(db, mid),
                           resumed=entry.get("resumed", False))
        entry.update(done=True, told=list(res.unparsed))
        db.set_json(LIVE_KEY, posts)  # marked first: a failed DM must not repeat the report every tick
        await react(bot, ident.channel_id, mid, reaction_for(res))
        await report(bot, db, ident, res, live=True)
        done += 1
    db.set_json(LIVE_KEY, posts)
    return done


def build_channel_router(db: Db, ident: Identity) -> Router:
    router = Router(name="channel")

    def ours(event) -> bool:
        chat = event.chat
        return chat.type == "channel" and ident.channel_id is not None and chat.id == ident.channel_id

    router.channel_post.filter(ours)
    router.edited_channel_post.filter(ours)

    async def process(message: Message, bot: Bot, edited: bool) -> None:
        text = message.text or message.caption
        if not text or untouched_plan(db, message.message_id, text):
            return  # the plan as the bot posted it; once the owner edits it, it is their workout
        res = ingest_post(db, message.message_id, message.date, text, message.edit_date if edited else None)
        if res.kind == "log" and res.sets:
            await track_workout(bot, db, ident, res)
            return
        forget_workout(db, message.message_id)  # an edit may have turned a workout into something else
        if emoji := reaction_for(res):
            await react(bot, message.chat.id, message.message_id, emoji)
        await report(bot, db, ident, res, live=not edited)

    @router.channel_post()
    async def on_post(message: Message, bot: Bot) -> None:
        await process(message, bot, edited=False)

    @router.edited_channel_post()
    async def on_edit(message: Message, bot: Bot) -> None:
        await process(message, bot, edited=True)

    @router.callback_query(F.data == PLAN_DONE)
    async def on_plan_done(cb: CallbackQuery, bot: Bot) -> None:
        post = cb.message
        if cb.from_user.id != ident.owner_id or not isinstance(post, Message) or post.chat.id != ident.channel_id:
            await cb.answer()
            return
        text = post.text or post.caption or ""
        if untouched_plan(db, post.message_id, text):
            await cb.answer("Программа ещё без правок: впиши, что сделал, и нажми снова. "
                            "Если уже правил — перешли этот пост мне.", show_alert=True)
            return
        res = ingest_post(db, post.message_id, post.date, text, post.edit_date or clock.now())
        if res.kind != "log" or not res.sets:
            await cb.answer("Не нашёл в посте ни одного подхода.", show_alert=True)
            return
        await cb.answer("Записал.")
        await track_workout(bot, db, ident, res, finished=True)
        try:
            await bot.edit_message_reply_markup(chat_id=post.chat.id, message_id=post.message_id,
                                                reply_markup=plan_markup(recorded=True))
        except TelegramAPIError as e:
            log.info("plan button not updated: %s", e)

    @router.my_chat_member()
    async def on_membership(update: ChatMemberUpdated, bot: Bot) -> None:
        chat, member = update.chat, update.new_chat_member
        if chat.type != "channel":
            return
        if member.status == "administrator":
            by_owner = update.from_user is not None and update.from_user.id == ident.owner_id
            if ident.channel_id is None and by_owner:
                ident.set_channel(chat.id)  # only the owner can bind a channel
            if chat.id != ident.channel_id:
                if not by_owner:
                    log.warning("ignored admin promotion in channel %s by user %s", chat.id,
                                update.from_user.id if update.from_user else None)
                return
            note = "" if getattr(member, "can_post_messages", False) else \
                "\n⚠️ Нет права «Публикация сообщений» — не смогу присылать программу в канал."
            await dm(bot, ident, f"Подключился к каналу «{html.escape(chat.title or '')}». "
                                 f"Пиши тренировки как раньше, команды — /help.{note}")
        elif chat.id == ident.channel_id and member.status in LOST:
            await dm(bot, ident, f"⚠️ Меня убрали из админов «{html.escape(chat.title or '')}» — "
                                 "новые посты не вижу. Верни права админа, и всё продолжится.")

    return router
