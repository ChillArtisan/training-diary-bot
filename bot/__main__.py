import asyncio
import html
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, ErrorEvent

from diary.db import Db
from diary.ingest import reparse_if_needed

from . import health, pending, ui
from .channel import build_channel_router
from .common import Alerts, Identity, dm
from .config import Config
from .owner import build_claim_router, build_fallback_router, build_owner_router
from .scheduler import scheduler_loop

log = logging.getLogger("bot")
DOWNTIME_NOTICE_MIN = 15
TELEGRAM_KEEPS_UPDATES_H = 23  # Telegram drops unprocessed updates after about 24 hours

COMMANDS = [
    ("plan", "программа на сегодня"), ("today", "программа в канал"), ("light", "плохой день: легче"),
    ("short", "мало времени"), ("swap", "чем заменить упражнение"), ("cycle", "где я в цикле"),
    ("skip", "пропустить день"), ("pause", "болею / уехал"), ("resume", "снять паузу"),
    ("avoid", "болит: убрать упражнение"), ("analytics", "аналитика с графиками"), ("progress", "график силы"),
    ("kpd", "КПД тренировок"), ("body", "вес и замеры"), ("stats", "общая картина"),
    ("week", "итоги недели"), ("goal", "цели и прогноз"), ("plates", "блины на сторону"), ("help", "все команды"),
]


def downtime_notice(minutes: float | None) -> str | None:
    """What to tell the owner after a restart; None when the gap was short or is unknown."""
    if minutes is None or minutes <= DOWNTIME_NOTICE_MIN:
        return None
    hours = minutes / 60
    gap = f"~{hours:.0f} ч" if minutes >= 120 else f"~{round(minutes)} мин"
    if hours > TELEGRAM_KEEPS_UPDATES_H:
        return (f"Был недоступен {gap}, снова работаю. Telegram хранит необработанное всего сутки, "
                "так что часть постов за это время я мог не увидеть. Перешли мне недостающие посты из канала "
                "или пришли экспорт канала (/import) — подтяну.")
    return f"Был недоступен {gap}, снова работаю. Посты за это время прочитаю сам."


async def startup(bot: Bot, db: Db, ident: Identity) -> None:
    n = reparse_if_needed(db)
    if n:
        log.info("re-parsed %d posts after a parser update", n)
    pending.prune(db)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS],
                              scope=BotCommandScopeAllPrivateChats())
    if ident.owner_id and db.get("keyboard_version") != ui.KEYBOARD_VERSION:
        if await dm(bot, ident, "Обновление: кнопки снизу, 📊 Аналитика с графиками, 📈 Прогресс по упражнениям "
                                "и КПД после каждой тренировки.", reply_markup=ui.MAIN):
            db.set("keyboard_version", ui.KEYBOARD_VERSION)
    if notice := downtime_notice(health.downtime_minutes(health.heartbeat_path(db.path))):
        await dm(bot, ident, notice)
    if ident.channel_id:
        try:
            me = await bot.get_chat_member(ident.channel_id, bot.id)
            if me.status != "administrator" or not getattr(me, "can_post_messages", False):
                await dm(bot, ident, "⚠️ В канале у меня нет прав админа с публикацией — проверь настройки канала.")
        except TelegramAPIError as e:
            await dm(bot, ident, f"⚠️ Не вижу канал дневника: {html.escape(str(e))}")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    health.ensure_writable(Path(cfg.db_path).parent)
    session = AiohttpSession(proxy=cfg.proxy) if cfg.proxy else AiohttpSession()
    poll = health.Health()
    session.middleware(health.PollTracker(poll))
    bot = Bot(cfg.token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    db = Db(cfg.db_path)
    ident = Identity(db, cfg)
    alerts = Alerts()
    if ident.owner_id is None:
        code = ident.new_pair_code()
        log.warning("Pair code: %s — send /start %s to the bot", code, code)

    dp = Dispatcher()
    dp.include_router(build_channel_router(db, ident))
    dp.include_router(build_claim_router(db, ident))
    dp.include_router(build_owner_router(db, ident, alerts))
    dp.include_router(build_fallback_router())

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        await alerts.report(bot, ident, "обработка сообщения", event.exception)
        return True

    jobs = watch = None
    try:
        await startup(bot, db, ident)
        jobs = asyncio.create_task(scheduler_loop(bot, db, ident, alerts, poll))
        watch = asyncio.create_task(health.watchdog(bot, poll, lambda text: dm(bot, ident, text)))
        # pending updates are kept: posts written while the bot was down still get parsed
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=True)
    finally:
        for task in (jobs, watch):
            if task:
                task.cancel()
        await bot.session.close()
        db.conn.close()  # checkpoints the WAL, so a restored or copied diary.db is a single file


if __name__ == "__main__":
    asyncio.run(main())
