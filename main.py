"""Точка входа: сборка бота, подключение к локальному Bot API Server и запуск polling."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from config import Settings
from database import Database
from handlers import router
from middlewares import OwnerOnlyMiddleware

# Общий таймаут HTTP-запросов к Bot API (сек). Большой, т.к. загрузка/отправка
# файлов до 2 ГБ через send_document может идти очень долго.
SESSION_TIMEOUT = 3600


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    logger = logging.getLogger("main")

    settings = Settings()
    settings.storage_dir.mkdir(parents=True, exist_ok=True)

    # PostgreSQL: подключение + автоматическое создание таблицы media_files
    db = Database(settings.db_url)
    await db.connect()

    # Локальный Telegram Bot API Server:
    #   {local_server_url}/bot{token}/{method}
    #   {local_server_url}/file/bot{token}/{path}
    # is_local=True включает режим локального сервера (абсолютные пути файлов,
    # загрузка файлов до 2 ГБ).
    api_server = TelegramAPIServer.from_base(settings.local_server_url, is_local=True)
    session = AiohttpSession(api=api_server, timeout=SESSION_TIMEOUT)

    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # db и settings попадают в хендлеры через dependency injection по имени аргумента
    dp = Dispatcher(db=db, settings=settings)

    # Безопасность: до любых хендлеров отсекаем всех, кроме владельца
    dp.update.outer_middleware(OwnerOnlyMiddleware(settings.allowed_user_id))
    dp.include_router(router)

    try:
        me = await bot.get_me()
        logger.info("Бот @%s запущен, сервер: %s", me.username, settings.local_server_url)
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger("main").info("Бот остановлен")
