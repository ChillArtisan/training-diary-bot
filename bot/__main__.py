import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, ErrorEvent

from diary.db import Db
from diary.ingest import reparse_if_needed

from . import ui
from .channel import build_channel_router
from .common import Alerts, Identity, dm, now_msk
from .config import Config
from .owner import build_claim_router, build_owner_router
from .scheduler import scheduler_loop

log = logging.getLogger("bot")
DOWNTIME_NOTICE_MIN = 15

COMMANDS = [
    ("plan", "программа на сегодня"), ("today", "программа в канал"), ("light", "плохой день: легче"),
    ("short", "мало времени"), ("swap", "чем заменить упражнение"), ("cycle", "где я в цикле"),
    ("skip", "пропустить день"), ("pause", "болею / уехал"), ("resume", "снять паузу"),
    ("avoid", "болит: убрать упражнение"), ("analytics", "аналитика с графиками"), ("progress", "график силы"),
    ("kpd", "КПД тренировок"), ("body", "вес и замеры"), ("stats", "общая картина"),
    ("week", "итоги недели"), ("plates", "блины на сторону"), ("help", "все команды"),
]


async def startup(bot: Bot, db: Db, ident: Identity) -> None:
    n = reparse_if_needed(db)
    if n:
        log.info("re-parsed %d posts after a parser update", n)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS],
                              scope=BotCommandScopeAllPrivateChats())
    if ident.owner_id and db.get("keyboard_version") != ui.KEYBOARD_VERSION:
        if await dm(bot, ident, "Обновление: кнопки снизу, 📊 Аналитика с графиками, 📈 Прогресс по упражнениям "
                                "и КПД после каждой тренировки.", reply_markup=ui.MAIN):
            db.set("keyboard_version", ui.KEYBOARD_VERSION)
    last_beat = (db.path.parent / "heartbeat")
    if last_beat.exists():
        down = (now_msk() - datetime.fromisoformat(last_beat.read_text())).total_seconds() / 60
        if down > DOWNTIME_NOTICE_MIN:
            await dm(bot, ident, f"Был недоступен ~{round(down)} мин, снова работаю. Посты за это время прочитаю сам.")
    if ident.channel_id:
        try:
            me = await bot.get_chat_member(ident.channel_id, bot.id)
            if me.status != "administrator" or not getattr(me, "can_post_messages", False):
                await dm(bot, ident, "⚠️ В канале у меня нет прав админа с публикацией — проверь настройки канала.")
        except TelegramAPIError as e:
            await dm(bot, ident, f"⚠️ Не вижу канал дневника: {e}")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    session = AiohttpSession(proxy=cfg.proxy) if cfg.proxy else None
    bot = Bot(cfg.token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    db = Db(cfg.db_path)
    ident = Identity(db, cfg)
    alerts = Alerts()

    dp = Dispatcher()
    dp.include_router(build_channel_router(db, ident))
    dp.include_router(build_claim_router(db, ident))
    dp.include_router(build_owner_router(db, ident))

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        await alerts.report(bot, ident, "обработка сообщения", event.exception)
        return True

    await startup(bot, db, ident)
    jobs = asyncio.create_task(scheduler_loop(bot, db, ident, alerts))
    try:
        # pending updates are kept: posts written while the bot was down still get parsed
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=True)
    finally:
        jobs.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
