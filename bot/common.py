"""Shared bot helpers: who the owner is, sending, plan posting, alerts."""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from diary import aliases
from diary.db import Db
from diary.ingest import MSK
from diary.today import build_plan, render_html

from .config import Config

log = logging.getLogger(__name__)
CHUNK = 3800  # Telegram allows 4096; leave room for markup


def now_msk() -> datetime:
    return datetime.now(MSK)


class Identity:
    """Owner and channel: from the environment if set, otherwise learned at runtime and kept in the DB."""

    def __init__(self, db: Db, cfg: Config):
        self.db, self.cfg = db, cfg

    @property
    def owner_id(self) -> int | None:
        if self.cfg.owner_id:
            return self.cfg.owner_id
        v = self.db.get("owner_id")
        return int(v) if v else None

    @property
    def channel_id(self) -> int | None:
        if self.cfg.channel_id:
            return self.cfg.channel_id
        v = self.db.get("channel_id")
        return int(v) if v else None

    def set_owner(self, uid: int) -> None:
        self.db.set("owner_id", str(uid))

    def set_channel(self, cid: int) -> None:
        self.db.set("channel_id", str(cid))

    def post_link(self, message_id: int) -> str:
        """Private channel link: t.me/c/<id without -100>/<message>."""
        return f"https://t.me/c/{str(abs(self.channel_id or 0))[3:]}/{message_id}"


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def chunks(text: str, size: int = CHUNK) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


async def send_long(bot: Bot, chat_id: int, text: str, **kw) -> None:
    parts = chunks(text)
    for i, part in enumerate(parts):
        await bot.send_message(chat_id, part, **(kw if i == len(parts) - 1 else {}))


async def dm(bot: Bot, ident: Identity, text: str, **kw) -> bool:
    if not ident.owner_id:
        return False
    try:
        await send_long(bot, ident.owner_id, text, **kw)
        return True
    except TelegramAPIError as e:
        log.warning("dm failed: %s", e)
        return False


async def post_plan(bot: Bot, db: Db, ident: Identity, mode: str = "normal") -> bool:
    if not ident.channel_id:
        return False
    now = now_msk()
    plan = build_plan(db, now.date(), mode=mode)
    if plan is None:
        return False
    for part in chunks(render_html(plan)):
        sent = await bot.send_message(ident.channel_id, part)
        db.mark_sent(sent.message_id, now.date().isoformat())
    return True


async def send_suggestion(bot: Bot, db: Db, chat_id: int, sid: int | None = None) -> bool:
    s = db.suggestion(sid) if sid else next(iter(db.open_suggestions()), None)
    if s is None or s["status"] != "open":
        return False
    left = len(db.open_suggestions())
    text = (f"«{html.escape(s['name'])}» — это «{html.escape(s['candidate'])}»?"
            + (f"\n<i>осталось вопросов: {left}</i>" if left > 1 else ""))
    await bot.send_message(chat_id, text, reply_markup=kb(
        [("Да, одно", f"merge:{s['id']}"), ("Нет, разные", f"keep:{s['id']}")]))
    return True


def find_exercise(db: Db, query: str) -> int | None:
    q = aliases.norm(query)
    if not q:
        return None
    if eid := db.exercise_by_alias(q):
        return eid
    eid, sc, _ = aliases.best_match(q, db.alias_map())
    if eid and sc >= aliases.ASK_SCORE:
        return eid
    hits = [x["id"] for x in db.exercises_used() if q in aliases.norm(x["name"])]
    return hits[0] if hits else None


class Alerts:
    """Error reports to the owner, at most one per kind per half hour."""

    def __init__(self, every: float = 1800):
        self.every = every
        self.sent: dict[str, float] = {}

    async def report(self, bot: Bot, ident: Identity, where: str, exc: BaseException) -> None:
        log.error("%s failed", where, exc_info=exc)
        key = f"{where}:{type(exc).__name__}"
        if time.monotonic() - self.sent.get(key, -self.every) < self.every:
            return
        self.sent[key] = time.monotonic()
        await dm(bot, ident, f"⚠️ Ошибка ({html.escape(where)}): {html.escape(type(exc).__name__)}: "
                             f"{html.escape(str(exc)[:300])}\nБот работает дальше.")
