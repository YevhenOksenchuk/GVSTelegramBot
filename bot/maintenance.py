"""Бекап бази: щоденний і на вимогу (/backup)."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import datetime
from typing import FrozenSet, Optional

from aiogram import Bot
from aiogram.types import FSInputFile

from .publisher import kyiv_now
from .storage import Storage

log = logging.getLogger(__name__)
LAST_BACKUP_DAY = "last_backup_day"
LAST_BACKUP_AT = "last_backup_at"


async def backup_db(storage: Storage, dest: str) -> int:
    """Консистентна копія бази у dest (VACUUM INTO — бот не зупиняється). Повертає розмір у байтах."""
    if os.path.exists(dest):
        os.remove(dest)
    await storage.db.execute("VACUUM INTO ?", (dest,))
    return os.path.getsize(dest)


async def send_backup(bot: Bot, storage: Storage, owner_ids: FrozenSet[int]) -> int:
    """Надіслати копію бази кожному власнику як файл. Повертає кількість успішних надсилань."""
    now = kyiv_now()
    name = f"bot-{now:%Y-%m-%d}.db"
    dest = os.path.join(tempfile.gettempdir(), name)
    size = await backup_db(storage, dest)
    chats = sum(n for _, n in await storage.stats())
    users = len(await storage.list_users())
    caption = (
        f"💾 Бекап бази за {now:%d.%m.%Y %H:%M}\n"
        f"{max(1, size // 1024)} КБ · активних чатів: {chats} · користувачів: {users}\n"
        "Відновлення: покласти файл як data/bot.db і перезапустити бота."
    )
    sent = 0
    try:
        for owner in owner_ids:
            try:
                await bot.send_document(owner, FSInputFile(dest, filename=name), caption=caption)
                sent += 1
            except Exception as e:
                log.warning("Не вдалося надіслати бекап власнику %s: %s", owner, e)
    finally:
        try:
            os.remove(dest)
        except OSError:
            pass
    if sent:
        await storage.set_meta(LAST_BACKUP_DAY, now.strftime("%Y-%m-%d"))
        await storage.set_meta(LAST_BACKUP_AT, now.strftime("%d.%m %H:%M"))
    return sent


def backup_due(now: datetime, hour: int, last_day: Optional[str]) -> bool:
    """Чи час щоденного бекапу: після hour:05 і сьогодні ще не робили (після рестарту — надолужуємо)."""
    after = now.hour > hour or (now.hour == hour and now.minute >= 5)
    return after and last_day != now.strftime("%Y-%m-%d")


async def backup_loop(bot: Bot, storage: Storage, owner_ids: FrozenSet[int], hour: int) -> None:
    """Щодня після hour:05 за Києвом — бекап власникам. Позначка в meta захищає від повтору після рестарту."""
    while True:
        try:
            if owner_ids and backup_due(kyiv_now(), hour, await storage.get_meta(LAST_BACKUP_DAY)):
                await send_backup(bot, storage, owner_ids)
        except Exception:
            log.exception("Помилка щоденного бекапу")
        await asyncio.sleep(60)
