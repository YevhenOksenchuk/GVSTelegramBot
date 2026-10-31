"""/month (лише власники): картинка-підсумок без світла за місяць, навігація по місяцях, «за весь час»,
публікація у вибраний канал чи групу."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import FrozenSet, List, Optional, Tuple

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message

from . import publisher
from .parser import Schedule
from .publisher import KYIV, _parse, send_schedule
from .stats import MONTH_NOM, MonthStats, all_time_stats, format_summary, month_stats, render_month_stats
from .storage import Storage

router = Router()
ALL = "all"
_KIND = {"channel": "📢", "group": "👥", "supergroup": "👥"}


def _keyboard(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text[:60], callback_data=data) for text, data in row] for row in rows
    ])


async def load_schedules(storage: Storage) -> Tuple[List[Schedule], Optional[date]]:
    """Усі графіки з бази та дата, з якої бот бачить канал."""
    rows = await storage.all_schedule_posts()
    schedules = [s for s in (_parse(text, datetime.fromisoformat(posted)) for _, text, _, posted in rows) if s]
    earliest = await storage.earliest_post_date()
    data_start = datetime.fromisoformat(earliest).astimezone(KYIV).date() if earliest else None
    return schedules, data_start


def stats_for(view: str, schedules: List[Schedule], data_start: Optional[date], today: date) -> Optional[MonthStats]:
    if view == ALL:
        return all_time_stats(schedules, today, data_start)
    year, month = (int(x) for x in view.split("-"))
    return month_stats(schedules, year, month, today, data_start)


def keyboard_for(view: str, data_start: Optional[date], today: date) -> InlineKeyboardMarkup:
    rows = []
    if view == ALL:
        rows.append([("Поточний місяць", f"mon:view:{today:%Y-%m}")])
    else:
        year, month = (int(x) for x in view.split("-"))
        first = date(year, month, 1)
        prev_last = first - timedelta(days=1)
        nav = []
        if data_start is None or prev_last >= data_start:
            nav.append((f"← {MONTH_NOM[prev_last.month]}", f"mon:view:{prev_last:%Y-%m}"))
        nxt = date(year, month, calendar.monthrange(year, month)[1]) + timedelta(days=1)
        if nxt <= today:
            nav.append((f"{MONTH_NOM[nxt.month]} →", f"mon:view:{nxt:%Y-%m}"))
        if nav:
            rows.append(nav)
        rows.append([("За весь час", f"mon:view:{ALL}")])
    rows.append([("📣 Опублікувати", f"mon:pub:{view}")])
    return _keyboard(rows)


async def build_view(storage: Storage, view: str) -> Optional[Tuple[bytes, str, InlineKeyboardMarkup]]:
    schedules, data_start = await load_schedules(storage)
    today = publisher.kyiv_now().date()
    st = stats_for(view, schedules, data_start, today)
    if st is None:
        return None
    return render_month_stats(st), format_summary(st), keyboard_for(view, data_start, today)


@router.message(Command("month"), F.chat.type == ChatType.PRIVATE)
async def cmd_month(message: Message, storage: Storage, owner_ids: FrozenSet[int]) -> None:
    if message.from_user.id not in owner_ids:
        return
    built = await build_view(storage, f"{publisher.kyiv_now():%Y-%m}")
    if not built:
        await message.answer("Ще немає жодного графіка — статистика з'явиться після першого.")
        return
    png, caption, keyboard = built
    await message.answer_photo(BufferedInputFile(png, filename="month.png"), caption=caption, parse_mode="HTML",
                               reply_markup=keyboard)


@router.callback_query(F.data.startswith("mon:"))
async def on_month_button(cb: CallbackQuery, storage: Storage, bot: Bot, owner_ids: FrozenSet[int]) -> None:
    if cb.from_user.id not in owner_ids:
        await cb.answer("Лише для власника бота", show_alert=True)
        return
    parts = cb.data.split(":")
    action = parts[1]
    chat_id, message_id = cb.message.chat.id, cb.message.message_id

    if action == "view":
        built = await build_view(storage, parts[2])
        if not built:
            await cb.answer("Немає даних за цей період", show_alert=True)
            return
        png, caption, keyboard = built
        media = InputMediaPhoto(media=BufferedInputFile(png, filename="month.png"), caption=caption, parse_mode="HTML")
        await bot.edit_message_media(media, chat_id=chat_id, message_id=message_id, reply_markup=keyboard)
        await cb.answer()
        return

    if action == "pub":  # вибір каналу/групи
        view = parts[2]
        chats = await storage.connected_chats()
        rows = [[(f"{_KIND.get(type_, '')} {title}", f"mon:to:{cid}:{view}")] for cid, title, type_ in chats]
        rows.append([("← Назад", f"mon:back:{view}")])
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=_keyboard(rows))
        await cb.answer("Куди опублікувати?" if chats else "Немає підключених каналів чи груп")
        return

    if action == "back":
        _, data_start = await load_schedules(storage)
        keyboard = keyboard_for(parts[2], data_start, publisher.kyiv_now().date())
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=keyboard)
        await cb.answer()
        return

    if action == "to":
        target, view = int(parts[2]), parts[3]
        built = await build_view(storage, view)
        if not built:
            await cb.answer("Немає даних за цей період", show_alert=True)
            return
        png, caption, keyboard = built
        title = await storage.get_chat_title(target) or str(target)
        sent = await send_schedule(bot, storage, target, png, caption)
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=keyboard)
        await cb.answer(f"✅ Опубліковано в {title}" if sent else f"⚠️ Не вдалося опублікувати в {title}",
                        show_alert=not sent)
        return

    await cb.answer()
