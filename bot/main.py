"""Точка входу: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeChat

from .config import load_config
from .access import router as access_router
from .handlers import router
from .publisher import Watcher
from .storage import Storage


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()

    storage = Storage(cfg.db_path)
    await storage.open()
    bot = Bot(cfg.bot_token)
    watcher = Watcher(bot, storage, cfg.source_channel, cfg.poll_interval)

    dp = Dispatcher()
    dp["storage"] = storage
    dp["watcher"] = watcher
    dp["owner_ids"] = cfg.owner_ids
    dp.include_router(access_router)
    dp.include_router(router)
    if not cfg.owner_ids:
        logging.warning("OWNER_ID не задано — приватний режим вимкнено, бот доступний усім")
    else:
        logging.info("Власники бота: %s", ", ".join(map(str, sorted(cfg.owner_ids))))

    commands = [
        BotCommand(command="now", description="Графік на сьогодні і завтра"),
        BotCommand(command="start", description="Підписатися / як додати в канал"),
        BotCommand(command="stop", description="Відписатися"),
    ]
    await bot.set_my_commands(commands)
    for owner in cfg.owner_ids:  # службові команди бачать лише власники
        try:
            await bot.set_my_commands(commands + [
                BotCommand(command="post", description="Опублікувати графік у канал/групу"),
            BotCommand(command="users", description="Хто має доступ"),
                BotCommand(command="stats", description="Стан бота"),
            ], scope=BotCommandScopeChat(chat_id=owner))
        except Exception as e:  # власник ще не натискав Start
            logging.warning("Не вдалося налаштувати меню для власника %s: %s", owner, e)

    task = asyncio.create_task(watcher.run())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
