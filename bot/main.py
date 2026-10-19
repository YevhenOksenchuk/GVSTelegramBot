"""Точка входу: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeChat

from . import publisher
from .access import router as access_router
from .alerts import Alerts
from .config import load_config
from .handlers import router
from .maintenance import backup_loop
from .publisher import Watcher, kyiv_now
from .storage import Storage

WATCHDOG_STALE = 600  # секунд без спроби перевірити канал — вважаємо, що бот завис


def watchdog_reason(watcher: Watcher, task_done: bool, started: datetime, now: datetime) -> Optional[str]:
    """Чому бот треба перезапустити; None — усе гаразд."""
    if task_done:
        return "цикл перевірки каналу завершився"
    if (now - started).total_seconds() <= WATCHDOG_STALE:
        return None  # щойно запустились — даємо час
    if watcher.last_check is None or (now - watcher.last_check).total_seconds() > WATCHDOG_STALE:
        return "перевірка каналу не запускалась понад 10 хв"
    return None


async def watchdog_loop(watcher: Watcher, watcher_task: asyncio.Task, alerts: Alerts) -> None:
    """Якщо цикл перевірки завис або впав — виходимо; Docker (restart: unless-stopped) підніме бота."""
    started = kyiv_now()
    while True:
        await asyncio.sleep(60)
        reason = watchdog_reason(watcher, watcher_task.done(), started, kyiv_now())
        if reason:
            logging.critical("Watchdog: %s — перезапуск", reason)
            await alerts.notify("watchdog", f"🔁 Бот перезапускається: {reason}.", ttl=0, timeout=5)
            os._exit(1)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    started_at = kyiv_now()

    storage = Storage(cfg.db_path)
    await storage.open()
    bot = Bot(cfg.bot_token)
    alerts = Alerts(bot, cfg.owner_ids)
    publisher.ALERTS = alerts
    watcher = Watcher(bot, storage, cfg.source_channel, cfg.poll_interval, cfg.healthcheck_url, cfg.owner_ids)

    dp = Dispatcher()
    dp["storage"] = storage
    dp["watcher"] = watcher
    dp["owner_ids"] = cfg.owner_ids
    dp["alerts"] = alerts
    dp["config"] = cfg
    dp["started_at"] = started_at
    dp.include_router(access_router)
    dp.include_router(router)
    logging.info("Версія: %s", cfg.bot_version)
    if not cfg.owner_ids:
        logging.warning("OWNER_ID не задано — приватний режим вимкнено, бот доступний усім")
    else:
        logging.info("Власники бота: %s", ", ".join(map(str, sorted(cfg.owner_ids))))
    if not cfg.healthcheck_url:
        logging.info("HEALTHCHECK_URL не задано — зовнішній нагляд вимкнено")

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
                BotCommand(command="fix", description="Перевидати графік у надісланих повідомленнях"),
                BotCommand(command="pin", description="Закріплення графіка по чатах"),
                BotCommand(command="backup", description="Надіслати бекап бази"),
            ], scope=BotCommandScopeChat(chat_id=owner))
        except Exception as e:  # власник ще не натискав Start
            logging.warning("Не вдалося налаштувати меню для власника %s: %s", owner, e)

    watcher_task = asyncio.create_task(watcher.run())
    tasks = [
        watcher_task,
        asyncio.create_task(backup_loop(bot, storage, cfg.owner_ids, cfg.backup_hour)),
        asyncio.create_task(watchdog_loop(watcher, watcher_task, alerts)),
    ]
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in tasks:
            task.cancel()
        await storage.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
