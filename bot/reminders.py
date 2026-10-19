"""«Моя черга»: нагадування власникам про відключення за їхньою підчергою.

Події рахуються щоразу з найсвіжішого графіка (оновлення обленерго враховуються автоматично),
а таблиця reminders_sent захищає від повторів після рестартів. Ключ події — абсолютний час
(день + година), тому нагадування про 00:10 завтра не дублюється після півночі.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, FrozenSet, List, Optional, Tuple

from aiogram import Bot

from .publisher import KYIV, kyiv_now, latest_by_day
from .storage import Storage

log = logging.getLogger(__name__)

LEAD_OPTIONS = (30, 20, 15, 10)          # за скільки хвилин нагадувати
BACK_WINDOW = timedelta(minutes=10)      # скільки після кінця відключення ще доречно «світло має з'явитися»
TICK = 30                                # секунд між перевірками

AbsInterval = Tuple[datetime, datetime]


@dataclass
class Event:
    kind: str                           # pre | back | cancel
    at: datetime                        # момент події — ключ від повторів
    start: datetime
    end: Optional[datetime] = None
    next_start: Optional[datetime] = None


def absolute_intervals(by_day: Dict[date, object], queue: str, days) -> List[AbsInterval]:
    """Відключення підчерги за кілька днів у абсолютному часі; суміжні через північ — злиті."""
    raw: List[AbsInterval] = []
    for day in sorted(days):
        found = by_day.get(day)
        if not found:
            continue
        schedule = found[0] if isinstance(found, tuple) else found
        base = datetime.combine(day, time(0), tzinfo=KYIV)
        for s, e in schedule.queues.get(queue, []):
            raw.append((base + timedelta(minutes=s), base + timedelta(minutes=e)))
    merged: List[AbsInterval] = []
    for s, e in sorted(raw):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def due_events(intervals: List[AbsInterval], now: datetime, lead_minutes: int, notify_back: bool,
               sent_pre: List[datetime]) -> List[Event]:
    """Події, актуальні саме зараз (без урахування вже надісланих — це перевіряє run_once)."""
    events: List[Event] = []
    lead = timedelta(minutes=lead_minutes)
    for i, (s, e) in enumerate(intervals):
        if s - lead <= now < s:
            events.append(Event("pre", s, s, e))
        if notify_back and e <= now < e + BACK_WINDOW:
            next_start = intervals[i + 1][0] if i + 1 < len(intervals) else None
            events.append(Event("back", e, s, e, next_start))
    starts = {s for s, _ in intervals}
    for t in sent_pre:  # попередили, а відключення зникло з графіка ще до початку
        if now < t and t not in starts:
            events.append(Event("cancel", t, t))
    return events


def _hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _day_word(dt: datetime, now: datetime) -> str:
    if dt.date() == now.date():
        return ""
    if dt.date() == now.date() + timedelta(days=1):
        return " завтра"
    return dt.strftime(" %d.%m")


def format_event(queue: str, ev: Event, now: datetime) -> str:
    if ev.kind == "pre":
        left = int((ev.start - now).total_seconds() // 60)
        when = "Вже зараз." if left <= 0 else f"Через {left} хв."
        until = _hm(ev.end) + (" завтра" if ev.end.date() > ev.start.date() and ev.end.time() != time(0) else "")
        return f"⚠️ Черга {queue}: о {_hm(ev.start)}{_day_word(ev.start, now)} вимкнуть світло (до {until}). {when}"
    if ev.kind == "back":
        if ev.next_start is None:
            tail = "Далі без відключень за відомим графіком."
        else:
            tail = f"Наступне відключення о {_hm(ev.next_start)}{_day_word(ev.next_start, now)}."
        return f"💡 Черга {queue}: за графіком світло має з'явитися о {_hm(ev.end)}. {tail}"
    return f"✅ Черга {queue}: відключення о {_hm(ev.start)}{_day_word(ev.start, now)} прибрали з графіка."


async def run_once(bot: Bot, storage: Storage, owner_ids: FrozenSet[int], now: Optional[datetime] = None) -> int:
    """Один прохід: кому з власників що треба нагадати саме зараз. Повертає кількість надісланих."""
    now = now or kyiv_now()
    prefs = [p for p in await storage.all_prefs() if p[0] in owner_ids]
    if not prefs:
        return 0
    by_day = await latest_by_day(storage)
    today = now.date()
    days = (today - timedelta(days=1), today, today + timedelta(days=1))
    sent = 0
    for user_id, queue, lead, notify_back in prefs:
        intervals = absolute_intervals(by_day, queue, days)
        sent_pre = [datetime.fromisoformat(t) for t in await storage.sent_reminder_times(user_id, "pre")]
        for ev in due_events(intervals, now, lead, notify_back, sent_pre):
            key = ev.at.isoformat(timespec="minutes")
            if await storage.reminder_sent(user_id, ev.kind, key):
                continue
            try:
                await bot.send_message(user_id, format_event(queue, ev, now))
            except Exception as e:
                log.warning("Не вдалося надіслати нагадування %s: %s", user_id, e)
                continue
            await storage.mark_reminder(user_id, ev.kind, key)
            sent += 1
    return sent


async def reminders_loop(bot: Bot, storage: Storage, owner_ids: FrozenSet[int]) -> None:
    while True:
        try:
            await run_once(bot, storage, owner_ids)
            await storage.prune_reminders()
        except Exception:
            log.exception("Помилка нагадувань")
        await asyncio.sleep(TICK)
