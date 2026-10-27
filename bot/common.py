"""Shared bot helpers: who the owner is, sending, plan posting, alerts."""
from __future__ import annotations

import html
import logging
import re
import secrets
import time
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from diary import aliases, clock, state
from diary.db import Db
from diary.today import build_plan, render_post

from .config import Config

log = logging.getLogger(__name__)
CHUNK = 3800  # Telegram allows 4096; leave room for markup


def now_msk() -> datetime:
    return clock.now()


class Identity:
    """Owner and channel: from the environment if set, otherwise learned at runtime and kept in the DB."""

    def __init__(self, db: Db, cfg: Config):
        self.db, self.cfg = db, cfg
        self.pair_code: str | None = None
        self._fails: list[float] = []

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

    def new_pair_code(self) -> str:
        """Random 6-digit code the first owner types as /start NNNNNN. Only ever logged, never sent."""
        self.pair_code = f"{secrets.randbelow(10 ** 6):06d}"
        return self.pair_code

    def try_claim(self, code: str, uid: int) -> bool:
        """Make uid the owner if the code is right; five wrong tries lock claiming for ten minutes."""
        if self.owner_id is not None or not self.pair_code:
            return False
        now = time.monotonic()
        self._fails = [t for t in self._fails if now - t < 600]
        if len(self._fails) >= 5:
            return False
        if not secrets.compare_digest(code.strip().encode(), self.pair_code.encode()):
            self._fails.append(now)
            return False
        self.set_owner(uid)
        self.pair_code = None
        return True

    def post_link(self, message_id: int) -> str:
        """Private channel link: t.me/c/<id without -100>/<message>."""
        return f"https://t.me/c/{str(abs(self.channel_id or 0))[3:]}/{message_id}"


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


TAG = re.compile(r"<(/?)([a-z-]+)[^>]*>")


def _track(stack: list[tuple[str, str]], text: str) -> None:
    """Keep the list of HTML tags still open after text."""
    for m in TAG.finditer(text):
        if m[1]:
            if stack and stack[-1][0] == m[2]:
                stack.pop()
        else:
            stack.append((m[2], m[0]))


def _closers(stack: list[tuple[str, str]]) -> str:
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def _cut(line: str, room: int) -> int:
    """Where to cut a long line: at a space, never inside a tag or an entity."""
    cut = min(room, len(line))
    if cut < len(line):
        lt = line.rfind("<", 0, cut)
        if lt != -1 and ">" not in line[lt:cut]:
            cut = lt
        amp = line.rfind("&", max(0, cut - 10), cut)
        if amp != -1 and ";" not in line[amp:cut]:
            cut = amp
        space = line.rfind(" ", 0, cut)
        if space > cut // 2:
            cut = space + 1
    return max(cut, 1)


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """Split HTML text on line boundaries; tags open at a split are closed and reopened in the next part."""
    out: list[str] = []
    stack: list[tuple[str, str]] = []  # tags open at the end of cur
    cur, fresh = "", True              # fresh: cur holds only reopened tags

    def flush() -> None:
        nonlocal cur, fresh
        out.append(cur + _closers(stack))
        cur, fresh = "".join(t for _, t in stack), True

    for line in text.split("\n"):
        while True:
            after = list(stack)
            _track(after, line)
            sep = 0 if fresh else 1
            if len(cur) + sep + len(line) + len(_closers(after)) <= size:
                cur = cur + ("\n" if sep else "") + line
                stack[:] = after
                fresh = False
                break
            if not fresh:
                flush()
                continue
            room = max(size - len(cur) - len(_closers(stack)) - 40, 100)
            cut = _cut(line, room)
            piece, line = line[:cut], line[cut:]
            cur += piece
            _track(stack, piece)
            flush()
            if not line:
                break
    if not fresh or not out:
        out.append(cur + _closers(stack))
    return out


async def send_long(bot: Bot, chat_id: int, text: str, **kw) -> None:
    parts = chunks(text)
    for i, part in enumerate(parts):
        await bot.send_message(chat_id, part, **(kw if i == len(parts) - 1 else {}))


async def dm(bot: Bot, ident: Identity, text: str, strict: bool = False, **kw) -> bool:
    """Send to the owner. False if there is no owner or the send failed; strict=True re-raises the failure."""
    if not ident.owner_id:
        return False
    try:
        await send_long(bot, ident.owner_id, text, **kw)
        return True
    except TelegramAPIError as e:
        log.warning("dm failed: %s", e)
        if strict:
            raise
        return False


async def post_plan(bot: Bot, db: Db, ident: Identity, mode: str = "normal") -> bool:
    if not ident.channel_id:
        return False
    now = now_msk()
    plan = build_plan(db, now.date(), mode=mode)
    if plan is None:
        return False
    state.note_plan(db, now.date(), plan)
    for part in chunks(render_post(plan)):  # no reasons or warm-up: those are in /plan
        sent = await bot.send_message(ident.channel_id, part, parse_mode=None)
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
    """Error reports to the owner, at most one per kind per half hour (counted from a delivered report)."""

    def __init__(self, every: float = 1800):
        self.every = every
        self.sent: dict[str, float] = {}

    async def report(self, bot: Bot, ident: Identity, where: str, exc: BaseException) -> None:
        log.error("%s failed", where, exc_info=exc)
        key = f"{where}:{type(exc).__name__}"
        last = self.sent.get(key)
        if last is not None and time.monotonic() - last < self.every:
            return
        if await dm(bot, ident, f"⚠️ Ошибка ({html.escape(where)}): {html.escape(type(exc).__name__)}: "
                                f"{html.escape(str(exc)[:300])}\nБот работает дальше."):
            self.sent[key] = time.monotonic()
