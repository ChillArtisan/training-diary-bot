"""What every owner handler module shares: the context and the callback/message helpers."""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from diary import clock
from diary.db import Db

from ..common import Alerts, Identity, find_exercise

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
/goal жим лёжа 100 к лету — цель и когда она будет по твоему темпу
/ex жим лёжа — последние подходы · /stats — общая картина · /week — итоги недели

<b>Зал</b>
/plates 82.5 — блины на сторону · /warmup 100 — разминка

<b>Порядок</b>
/merge — похожие названия · /alias икры стоя = Подъём на носки стоя
/equip жим ногами = тренажёр · /forget &lt;ссылка&gt; — забыть пост
/import — пришли файл экспорта · /export — CSV · /backup — копия базы
/rebind — другой канал · /settings · /set ключ значение · /status

Тренировку можно прислать и сюда — я выложу её в канал. Пост из канала можно переслать — я его учту. \
Главное — на кнопках снизу."""


@dataclass
class Ctx:
    db: Db
    ident: Identity
    alerts: Alerts = field(default_factory=Alerts)

    @staticmethod
    def today() -> date:
        return clock.today()

    async def need_exercise(self, message: Message, query: str | None, example: str) -> int | None:
        if not query or not query.strip():
            await message.answer(f"Например: {example}")
            return None
        eid = find_exercise(self.db, query)
        if eid is None:
            await message.answer(f"Не нашёл «{html.escape(query.strip())}». Как оно записано в канале?")
        return eid


def is_accessible(cb: CallbackQuery) -> bool:
    return isinstance(cb.message, Message)


def chat_id_of(cb: CallbackQuery) -> int:
    """Where to answer: the button's chat, or the user's private chat when the message is gone."""
    return cb.message.chat.id if cb.message is not None else cb.from_user.id


async def edit(cb: CallbackQuery, bot: Bot, text: str, **kw) -> None:
    """Replace the button's message; if it cannot be edited (gone, inaccessible), send a new one instead."""
    if is_accessible(cb):
        try:
            await cb.message.edit_text(text, **kw)
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return
    await bot.send_message(chat_id_of(cb), text, **kw)


async def reply(cb: CallbackQuery, bot: Bot, text: str, **kw) -> None:
    """A new message in the button's chat (works for inaccessible messages too)."""
    await bot.send_message(chat_id_of(cb), text, **kw)
