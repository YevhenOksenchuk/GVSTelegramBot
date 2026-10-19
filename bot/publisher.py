"""Перевірка каналу-джерела й розсилка графіка підписникам."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from html import escape
from typing import Dict, FrozenSet, List, Optional, Tuple

import httpx
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramRetryAfter,
)
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from .alerts import Alerts
from .announcements import format_announcement, is_announcement
from .formatter import format_caption
from .parser import Schedule, parse_post, unrecognized_lines
from .render import render_schedule
from .scraper import Post, fetch_posts
from .storage import Storage, text_hash

log = logging.getLogger(__name__)
KYIV = ZoneInfo("Europe/Kyiv")
SOURCE_DOWN_AFTER = 600  # секунд без доступу до каналу обленерго — алерт власникам

ALERTS: Optional[Alerts] = None  # встановлює main.py; у тестах може лишатися None


async def _alert(key: str, text: str, ttl: int = 3600) -> None:
    if ALERTS is not None:
        await ALERTS.notify(key, text, ttl)


def _looks_like_schedule(text: str) -> bool:
    return bool(re.search(r"гпв|графік", text, re.I) and re.search(r"відключ", text, re.I))


def kyiv_now() -> datetime:
    return datetime.now(KYIV)


def build_message(schedule: Schedule, url: str) -> Tuple[Optional[bytes], str]:
    """(PNG, підпис). Якщо картинка не малюється — PNG = None, і піде лише текст."""
    caption = format_caption(schedule, url)
    try:
        return render_schedule(schedule), caption
    except Exception:
        log.exception("Не вдалося намалювати графік на %s — надсилаю лише текст", schedule.date_label)
        return None, caption


def _parse(text: str, posted_at: datetime) -> Optional[Schedule]:
    published = posted_at.astimezone(KYIV)
    schedule = parse_post(text, published.date())
    if schedule:
        schedule.published = published
    return schedule


async def load_schedule(
    storage: Storage, post_id: int, text: str, posted_at: datetime, prev_text: Optional[str] = None
) -> Optional[Schedule]:
    """Розбирає пост і порівнює з попередньою версією графіка на ту саму дату.

    prev_text — старий текст цього ж посту, якщо його відредагували.
    """
    schedule = _parse(text, posted_at)
    if not schedule:
        return None
    schedule.post_id = post_id
    candidates = [(prev_text, posted_at.isoformat())] if prev_text else []
    candidates += await storage.schedule_posts_before(post_id)
    for old_text, old_posted in candidates:
        previous = _parse(old_text, datetime.fromisoformat(old_posted))
        if previous and previous.day == schedule.day:
            schedule.mark_changes(previous)
            break
    return schedule


async def has_newer_for_day(storage: Storage, post_id: int, schedule: Schedule) -> bool:
    """Чи є новіший пост з графіком на ту саму дату (тоді правка старого посту неактуальна)."""
    for text, posted_at in await storage.schedule_posts_after(post_id):
        newer = _parse(text, datetime.fromisoformat(posted_at))
        if newer and newer.day == schedule.day:
            return True
    return False


async def latest_by_day(storage: Storage, limit: int = 30) -> Dict[date, Tuple[Schedule, str]]:
    """Найсвіжіший графік на кожну дату серед останніх постів: дата -> (графік, посилання)."""
    latest: Dict[date, Tuple[Schedule, str]] = {}
    for post_id, text, url, posted_at in await storage.recent_schedule_posts(limit):
        quick = _parse(text, datetime.fromisoformat(posted_at))  # без порівняння з попередніми — дешево
        if not quick or not quick.day or quick.day in latest:
            continue
        schedule = await load_schedule(storage, post_id, text, datetime.fromisoformat(posted_at))
        if schedule:
            latest[schedule.day] = (schedule, url)
    return latest


async def current_schedules(storage: Storage) -> List[Tuple[Schedule, str]]:
    """Актуальні графіки: найсвіжіший на кожну дату від сьогодні, за зростанням дати.

    Якщо таких немає — останній відомий графік.
    """
    today = kyiv_now().date()
    latest = await latest_by_day(storage)
    future = [latest[d] for d in sorted(latest) if d >= today]
    if future:
        return future
    return [latest[max(latest)]] if latest else []


async def latest_schedule_for_day(storage: Storage, day: date) -> Optional[Tuple[Schedule, str]]:
    return (await latest_by_day(storage)).get(day)


async def deactivate_with_alert(storage: Storage, chat_id: int, reason: str) -> None:
    """Вимкнути чат і повідомити власників (лише канали/групи: блокування в особистих — не проблема)."""
    title = await storage.get_chat_title(chat_id) or str(chat_id)
    chat_type = await storage.get_chat_type(chat_id)
    await storage.deactivate_chat(chat_id)
    if chat_type == "private":
        return
    await _alert(
        f"lost:{chat_id}",
        f"⚠️ Бот втратив доступ до <b>{escape(title)}</b> — чат вимкнено.\n<i>{escape(reason)}</i>\n"
        "Якщо це помилка — додайте бота знову або поверніть йому права.",
    )


async def send_schedule(
    bot: Bot, storage: Storage, chat_id: int, png: Optional[bytes], caption: str,
    post_id: Optional[int] = None, day: Optional[date] = None,
) -> Optional[int]:
    """Надіслати графік (фото з підписом або лише текст). Повертає message_id, якщо вдалося.

    post_id/day — щоб запам'ятати повідомлення для закріплення та /fix.
    """
    for _ in range(3):
        try:
            if png is None:
                msg = await bot.send_message(chat_id, caption, parse_mode="HTML", disable_web_page_preview=True)
            else:
                msg = await bot.send_photo(
                    chat_id,
                    BufferedInputFile(png, filename="grafik.png"),
                    caption=caption,
                    parse_mode="HTML",
                )
            message_id = getattr(msg, "message_id", None)
            if message_id and post_id is not None:
                await storage.record_sent(
                    chat_id, message_id, post_id, day.isoformat() if day else None,
                    "text" if png is None else "photo",
                )
            return message_id
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramMigrateToChat as e:
            await storage.migrate_chat(chat_id, e.migrate_to_chat_id)
            chat_id = e.migrate_to_chat_id
        except TelegramForbiddenError as e:
            log.info("Чат %s заблокував бота — деактивую", chat_id)
            await deactivate_with_alert(storage, chat_id, str(e))
            return None
        except TelegramBadRequest as e:
            if "chat not found" in str(e).lower() or "not enough rights" in str(e).lower():
                log.info("Чат %s недоступний (%s) — деактивую", chat_id, e)
                await deactivate_with_alert(storage, chat_id, str(e))
            else:
                log.warning("Не вдалося надіслати в %s: %s", chat_id, e)
            return None
        except Exception:  # мережа тощо — пропускаємо цей чат, решта отримає
            log.exception("Помилка надсилання в %s", chat_id)
            return None
    return None


PIN_RETRY_AFTER = 86400  # секунд: без прав на закріплення не пробуємо знову цілу добу


async def maybe_pin(bot: Bot, storage: Storage, chat_id: int, chat_type: str, message_id: int) -> bool:
    """Закріпити графік у каналі/групі (тихо) і зняти попереднє закріплення бота."""
    if chat_type == "private":
        return False
    pinned, enabled, failed_at = await storage.pin_state(chat_id)
    if not enabled:
        return False
    if failed_at and (kyiv_now() - datetime.fromisoformat(failed_at)).total_seconds() < PIN_RETRY_AFTER:
        return False
    try:
        await bot.pin_chat_message(chat_id, message_id, disable_notification=True)
    except Exception as e:
        await storage.set_pin_failed(chat_id, kyiv_now().isoformat())
        title = await storage.get_chat_title(chat_id) or str(chat_id)
        right = "«Редагувати повідомлення інших»" if chat_type == "channel" else "«Закріплювати повідомлення»"
        await _alert(
            f"pin:{chat_id}",
            f"📌 Не можу закріпити графік у <b>{escape(title)}</b>. Дайте боту право {right} "
            f"або вимкніть закріплення для цього чату командою /pin.\n<i>{escape(str(e))}</i>",
            ttl=7 * 86400,
        )
        return False
    if pinned and pinned != message_id:
        try:
            await bot.unpin_chat_message(chat_id, message_id=pinned)
        except Exception:
            pass  # уже відкріпили вручну
    await storage.set_pinned(chat_id, message_id)
    return True


async def publish_current(bot: Bot, storage: Storage, chat_id: int, pin: bool = False) -> int:
    """Надіслати в чат актуальні графіки (сьогодні й завтра). Повертає кількість надісланих."""
    sent, last_id = 0, None
    for schedule, url in await current_schedules(storage):
        png, caption = build_message(schedule, url)
        message_id = await send_schedule(bot, storage, chat_id, png, caption, schedule.post_id, schedule.day)
        if message_id:
            sent, last_id = sent + 1, message_id
    if pin and last_id:
        await maybe_pin(bot, storage, chat_id, await storage.get_chat_type(chat_id) or "private", last_id)
    return sent


async def broadcast(bot: Bot, storage: Storage, schedule: Schedule, url: str) -> int:
    png, caption = build_message(schedule, url)
    sent = 0
    for chat_id, chat_type in await storage.active_chats_typed():
        message_id = await send_schedule(bot, storage, chat_id, png, caption, schedule.post_id, schedule.day)
        if message_id:
            sent += 1
            await maybe_pin(bot, storage, chat_id, chat_type, message_id)
        await asyncio.sleep(0.05)
    log.info("Графік на %s розіслано в %d чатів", schedule.date_label, sent)
    return sent


async def fix_messages(bot: Bot, storage: Storage, day: date) -> Tuple[int, List[Tuple[str, str]]]:
    """Замінити картинку й текст у вже надісланих повідомленнях за дату на актуальну версію графіка.

    Повертає (скільки оновлено, [(назва чату, помилка), ...]).
    """
    found = await latest_schedule_for_day(storage, day)
    if not found:
        return 0, [("—", "немає графіка на цю дату")]
    schedule, url = found
    png, caption = build_message(schedule, url)
    ok, failures = 0, []
    for chat_id, message_id, kind in await storage.latest_sent_per_chat(day.isoformat()):
        title = await storage.get_chat_title(chat_id) or str(chat_id)
        try:
            if kind == "photo" and png is not None:
                media = InputMediaPhoto(media=BufferedInputFile(png, filename="grafik.png"),
                                        caption=caption, parse_mode="HTML")
                await bot.edit_message_media(media, chat_id=chat_id, message_id=message_id)
            elif kind == "photo":
                await bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=caption,
                                               parse_mode="HTML")
            else:
                await bot.edit_message_text(caption, chat_id=chat_id, message_id=message_id, parse_mode="HTML",
                                            disable_web_page_preview=True)
            ok += 1
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                ok += 1  # уже актуальне
            else:
                failures.append((title, str(e)))
        except Exception as e:
            failures.append((title, str(e)))
        await asyncio.sleep(0.05)
    return ok, failures


async def propose_announcement(bot: Bot, storage: Storage, owner_ids: FrozenSet[int], post: Post) -> None:
    """Надіслати власникам пост-оголошення з кнопками «Опублікувати всім / Пропустити»."""
    if not owner_ids or await storage.announcement_status(post.post_id) is not None:
        return
    await storage.set_announcement(post.post_id, "pending")
    quote = post.text[:1500] + ("…" if len(post.text) > 1500 else "")
    text = (f"📰 <b>Оголошення обленерго</b> — опублікувати в усі чати?\n\n"
            f"<i>{escape(quote)}</i>\n\n{post.url}")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📣 Опублікувати всім", callback_data=f"ann:pub:{post.post_id}"),
        InlineKeyboardButton(text="🙈 Пропустити", callback_data=f"ann:skip:{post.post_id}"),
    ]])
    for owner in owner_ids:
        try:
            msg = await bot.send_message(owner, text, parse_mode="HTML", reply_markup=keyboard,
                                         disable_web_page_preview=True)
            await storage.save_prompt("announcement", post.post_id, owner, msg.message_id)
        except Exception as e:
            log.warning("Не вдалося надіслати оголошення власнику %s: %s", owner, e)


async def publish_announcement(bot: Bot, storage: Storage, post_id: int) -> int:
    """Опублікувати оголошення текстом в усі активні чати. Повертає кількість чатів."""
    row = await storage.get_post(post_id)
    if not row:
        return 0
    text, url, _ = row
    body = format_announcement(text, url)
    sent = 0
    for chat_id, _ in await storage.active_chats_typed():
        if await send_schedule(bot, storage, chat_id, None, body, post_id, None):
            sent += 1
        await asyncio.sleep(0.05)
    log.info("Оголошення %s розіслано в %d чатів", post_id, sent)
    return sent


class Watcher:
    def __init__(self, bot: Bot, storage: Storage, channel: str, interval: int, healthcheck_url: str = "",
                 owner_ids: FrozenSet[int] = frozenset()):
        self.bot, self.storage, self.channel, self.interval = bot, storage, channel, interval
        self.healthcheck_url = healthcheck_url
        self.owner_ids = owner_ids
        self.last_check: Optional[datetime] = None    # остання спроба (для /stats і сторожового таймера)
        self.last_success: Optional[datetime] = None  # остання вдала перевірка
        self.last_error: Optional[str] = None
        self.failing_since: Optional[datetime] = None

    async def check_once(self, client: httpx.AsyncClient) -> None:
        first_run = not await self.storage.has_any_posts()
        to_publish = {}  # дата -> (графік, посилання): по одному, найсвіжішому, на кожну дату
        for post in await fetch_posts(client, self.channel):
            old_hash = await self.storage.get_post_hash(post.post_id)
            if old_hash == text_hash(post.text):
                continue
            prev_text = await self.storage.get_post_text(post.post_id) if old_hash else None
            schedule = await load_schedule(
                self.storage, post.post_id, post.text, post.posted_at, prev_text
            )
            await self.storage.save_post(
                post.post_id, post.text, post.url, post.posted_at.isoformat(), schedule is not None
            )
            if first_run:
                continue
            await self.report_post_problems(post, schedule)
            if schedule is None:
                if old_hash is None and is_announcement(post.text):  # новий пост без графіка — на модерацію
                    await propose_announcement(self.bot, self.storage, self.owner_ids, post)
                continue
            if schedule.day and schedule.day < kyiv_now().date():
                continue  # старий графік — не спамимо
            if schedule.has_previous and not schedule.changed:
                continue  # той самий графік (правка тексту або повтор) — не дублюємо
            if old_hash and await has_newer_for_day(self.storage, post.post_id, schedule):
                continue  # відредагували старий пост, а на цю дату вже є новіший графік
            to_publish.pop(schedule.day, None)
            to_publish[schedule.day] = (schedule, post.url)
        if first_run:
            log.info("Перший запуск: наявні пости позначено як прочитані")
        # Кілька постів за одну перевірку: на кожну дату шлемо лише найсвіжіший, у порядку публікації.
        for schedule, url in to_publish.values():
            await broadcast(self.bot, self.storage, schedule, url)

    async def report_post_problems(self, post: Post, schedule: Optional[Schedule]) -> None:
        """Алерти власникам: рядки нового формату або графік-картинка, які бот не прочитає."""
        bad = unrecognized_lines(post.text)
        if bad:
            shown = "\n".join(escape(line) for line in bad[:5])
            await _alert(
                f"parse:{post.post_id}",
                f"⚠️ Не розпізнав рядки графіка (можливо, новий формат):\n<code>{shown}</code>\n{post.url}",
                ttl=86400,
            )
        if schedule is None and post.has_photo and _looks_like_schedule(post.text):
            await _alert(
                f"photo:{post.post_id}",
                f"⚠️ Схоже, графік опубліковано картинкою, а не текстом — бот його не прочитає:\n{post.url}",
                ttl=86400,
            )

    async def on_success(self, client: Optional[httpx.AsyncClient] = None) -> None:
        self.last_error = None
        self.last_success = kyiv_now()
        if self.failing_since is not None:
            down_for = (self.last_success - self.failing_since).total_seconds()
            self.failing_since = None
            if down_for >= SOURCE_DOWN_AFTER:
                if ALERTS is not None:
                    ALERTS.reset("source_down")
                await _alert(
                    "source_up", f"✅ Канал обленерго знову доступний (не було {int(down_for // 60)} хв).", ttl=0
                )
        if self.healthcheck_url and client is not None:  # зовнішній нагляд (healthchecks.io)
            try:
                await client.get(self.healthcheck_url, timeout=5)
            except Exception as e:
                log.info("Пінг healthcheck не вдався: %s", e)

    async def on_failure(self, error: str) -> None:
        self.last_error = error
        now = kyiv_now()
        if self.failing_since is None:
            self.failing_since = now
            return
        down_for = (now - self.failing_since).total_seconds()
        if down_for >= SOURCE_DOWN_AFTER:
            await _alert(
                "source_down",
                f"⚠️ Не можу прочитати канал обленерго вже {int(down_for // 60)} хв.\n<i>{escape(error)}</i>",
                ttl=6 * 3600,
            )

    async def run(self) -> None:
        async with httpx.AsyncClient() as client:
            while True:
                self.last_check = kyiv_now()
                try:
                    await self.check_once(client)
                    await self.on_success(client)
                except Exception as e:  # мережа, зміна верстки тощо — не падаємо
                    log.exception("Помилка перевірки каналу")
                    await self.on_failure(f"{type(e).__name__}: {e}")
                await asyncio.sleep(self.interval)
