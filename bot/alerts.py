"""Сповіщення власникам про проблеми (із захистом від повторів)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, FrozenSet, Optional

from aiogram import Bot

log = logging.getLogger(__name__)


class Alerts:
    def __init__(self, bot: Optional[Bot], owner_ids: FrozenSet[int]):
        self.bot, self.owner_ids = bot, owner_ids
        self._last: Dict[str, float] = {}

    async def notify(self, key: str, text: str, ttl: int = 3600, timeout: float = 15) -> bool:
        """Надіслати текст усім власникам в особисті. Той самий key — не частіше ніж раз на ttl секунд."""
        now = time.monotonic()
        if ttl and now - self._last.get(key, -1e12) < ttl:
            return False
        self._last[key] = now
        log.warning("ALERT [%s] %s", key, text)
        if not self.bot or not self.owner_ids:
            return False
        sent = False
        for owner in self.owner_ids:
            try:
                await asyncio.wait_for(
                    self.bot.send_message(owner, text, parse_mode="HTML", disable_web_page_preview=True),
                    timeout=timeout,
                )
                sent = True
            except Exception as e:  # власник ще не натискав Start, мережа тощо
                log.info("Не вдалося надіслати алерт власнику %s: %s", owner, e)
        return sent

    def reset(self, key: str) -> None:
        """Дозволити наступний алерт із цим key одразу (напр. після відновлення)."""
        self._last.pop(key, None)
