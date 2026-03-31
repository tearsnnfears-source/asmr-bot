import asyncio
import logging
import os
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from database import init_db
from utils.middleware import DatabaseMiddleware
from utils.scheduler import setup_scheduler
from handlers import start, cabinet, payment, group, admin, favorites
from handlers import poster as poster_handler
from webhook import create_app
from config import BOT_TOKEN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.update.middleware(DatabaseMiddleware())

    dp.include_router(group.router)
    dp.include_router(start.router)
    dp.include_router(cabinet.router)
    dp.include_router(payment.router)
    dp.include_router(admin.router)
    dp.include_router(favorites.router)
    dp.include_router(poster_handler.router)

    await init_db()
    logger.info("Database initialized")

    scheduler = setup_scheduler(bot)
    scheduler.start()
    logger.info("Scheduler started")

    # Запускаем webhook-сервер
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Webhook server started on port {port}")

    logger.info("Bot is running...")

    # Запускаем бота и сервер параллельно
    await asyncio.gather(
        dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    )


if __name__ == "__main__":
    asyncio.run(main())