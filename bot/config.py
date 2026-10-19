from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import FrozenSet

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_ids: FrozenSet[int]
    source_channel: str
    poll_interval: int
    db_path: str


def parse_owner_ids(raw: str) -> FrozenSet[int]:
    """«111111111,222222222» (через кому або пробіл) -> {111111111, 222222222}; 0 ігнорується."""
    return frozenset(int(x) for x in re.split(r"[,\s;]+", raw.strip()) if x and int(x))


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN не задано (див. .env.example)")
    return Config(
        bot_token=token,
        owner_ids=parse_owner_ids(os.getenv("OWNER_ID", "")),
        source_channel=os.getenv("SOURCE_CHANNEL", "pat_cherkasyoblenergo").lstrip("@"),
        poll_interval=max(20, int(os.getenv("POLL_INTERVAL", "60"))),
        db_path=os.getenv("DB_PATH", "data/bot.db"),
    )
