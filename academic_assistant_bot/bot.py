import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from database.db import engine, init_db
from handlers import setup_routers
from services.file_storage import FileStorageService
from services.gemini_service import GeminiTutorService
from services.hemis_client import HemisClient
from services.reminder_service import ReminderService


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        stream=sys.stdout,
    )


async def main() -> None:
    setup_logging()
    logger = logging.getLogger("bot")

    # 1. Инициализация базы данных SQLite с режимом WAL
    await init_db()

    # 2. Инициализация Telegram-бота и диспетчера
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # 3. Инициализация сервисов
    hemis_client = HemisClient()
    gemini_service = GeminiTutorService()
    file_storage = FileStorageService(hemis_client=hemis_client)
    reminder_service = ReminderService(bot=bot, gemini_service=gemini_service)

    dp["hemis_client"] = hemis_client
    dp["gemini_service"] = gemini_service
    dp["file_storage"] = file_storage
    dp["reminder_service"] = reminder_service

    dp.include_router(setup_routers())

    # 4. Запуск планировщика APScheduler и восстановление напоминаний из БД
    await reminder_service.start()

    try:
        await hemis_client.authenticate()
    except Exception as exc:
        logger.warning("Initial HEMIS auth check warning (will retry on requests): %s", exc)

    logger.info("Academic Assistant Bot v2.0 started and polling updates...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        logger.info("Shutting down services...")
        await reminder_service.shutdown()
        await hemis_client.close()
        await engine.dispose()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
