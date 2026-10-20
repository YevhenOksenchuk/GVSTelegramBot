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

from .parser import QUEUES
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


# ---------------------------------------------------------------------------
# Нагадування в каналах і групах: одне повідомлення на всі черги, що вимикаються в один час
# ---------------------------------------------------------------------------

QueueEnd = Tuple[str, datetime]  # (підчерга, кінець відключення)
_USERNAME_CHECKED: set = set()   # чати, для яких уже пробували дізнатися @username
QUIET_HOURS = (0, 5)             # «комендантська година»: нагадування в чати без звуку з 00:00 до 05:00


def is_quiet(now: datetime) -> bool:
    return QUIET_HOURS[0] <= now.hour < QUIET_HOURS[1]


def chat_due_groups(by_day: Dict[date, object], days, now: datetime, lead_minutes: int
                    ) -> List[Tuple[datetime, List[QueueEnd]]]:
    """[(початок, [(черга, кінець), ...]), ...] — відключення, про які час нагадати саме зараз."""
    lead = timedelta(minutes=lead_minutes)
    groups: Dict[datetime, List[QueueEnd]] = {}
    for queue in QUEUES:
        for s, e in absolute_intervals(by_day, queue, days):
            if s - lead <= now < s:
                groups.setdefault(s, []).append((queue, e))
    return [(s, groups[s]) for s in sorted(groups)]


def message_link(chat_id: int, chat_type: str, username: Optional[str], message_id: Optional[int]) -> Optional[str]:
    """Посилання на повідомлення бота в чаті (публічний — через @username, приватний — через t.me/c/)."""
    if not message_id:
        return None
    if username:
        return f"https://t.me/{username}/{message_id}"
    if chat_type in ("channel", "supergroup") and str(chat_id).startswith("-100"):
        return f"https://t.me/c/{str(chat_id)[4:]}/{message_id}"
    return None  # у звичайній групі посилань на повідомлення немає


def _end_label(start: datetime, end: datetime) -> str:
    if end.date() == start.date():
        return _hm(end)
    if end.time() == time(0) and end.date() == start.date() + timedelta(days=1):
        return "24:00"
    return f"{_hm(end)} наступного дня"


def _queues_label(queues: List[str]) -> str:
    return ("Черга " if len(queues) == 1 else "Черги ") + ", ".join(queues)


def format_chat_reminder(start: datetime, items: List[QueueEnd], now: datetime, link: Optional[str]) -> str:
    left = int((start - now).total_seconds() // 60)
    when = "вже зараз" if left <= 0 else f"через {left} хв"
    queues = [q for q, _ in items]
    ends = sorted({e for _, e in items})
    lines = [f"💡 <b>{_queues_label(queues)}</b> — без світла {when}", ""]
    since = _hm(start) + _day_word(start, now)
    if len(ends) == 1:
        lines.append(f"Електроенергії не буде з {since} до {_end_label(start, ends[0])}")
    else:
        lines.append(f"Електроенергії не буде з {since}:")
        for end in ends:
            same = [q for q, e in items if e == end]
            lines.append(f"• {', '.join(same)} — до {_end_label(start, end)}")
    if link:
        lines += ["", f'📋 <a href="{link}">Переглянути актуальний графік</a>']
    return "\n".join(lines)


async def run_chat_reminders_once(bot: Bot, storage: Storage, now: Optional[datetime] = None) -> int:
    """Нагадування в канали/групи, де їх увімкнено (/remind). Повертає кількість надісланих повідомлень."""
    now = now or kyiv_now()
    chats = await storage.chats_with_reminders()
    if not chats:
        return 0
    by_day = await latest_by_day(storage)
    today = now.date()
    days = (today - timedelta(days=1), today, today + timedelta(days=1))
    sent = 0
    for chat_id, chat_type, username, lead in chats:
        if username is None and chat_id not in _USERNAME_CHECKED:  # старі записи без @username — дізнаємось раз
            _USERNAME_CHECKED.add(chat_id)
            try:
                username = getattr(await bot.get_chat(chat_id), "username", None)
                if username:
                    await storage.set_chat_username(chat_id, username)
            except Exception as e:
                log.info("Не вдалося отримати username чату %s: %s", chat_id, e)
        for start, items in chat_due_groups(by_day, days, now, lead):
            key = start.isoformat(timespec="minutes")
            fresh = [(q, e) for q, e in items if not await storage.chat_reminder_sent(chat_id, q, key)]
            if not fresh:
                continue
            link = message_link(chat_id, chat_type, username,
                                await storage.latest_sent_message(chat_id, start.date().isoformat()))
            try:
                await bot.send_message(chat_id, format_chat_reminder(start, fresh, now, link),
                                       parse_mode="HTML", disable_web_page_preview=True,
                                       disable_notification=is_quiet(now))
            except Exception as e:
                log.warning("Не вдалося надіслати нагадування в чат %s: %s", chat_id, e)
                continue
            await storage.mark_chat_reminder(chat_id, [q for q, _ in fresh], key)
            sent += 1
    return sent


async def reminders_loop(bot: Bot, storage: Storage, owner_ids: FrozenSet[int]) -> None:
    while True:
        try:
            await run_once(bot, storage, owner_ids)
            await run_chat_reminders_once(bot, storage)
            await storage.prune_reminders()
        except Exception:
            log.exception("Помилка нагадувань")
        await asyncio.sleep(TICK)
