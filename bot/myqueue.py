"""Команда /my (лише власники): обрати свою підчергу й налаштувати нагадування."""
from __future__ import annotations

from datetime import timedelta
from typing import FrozenSet, Tuple

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import publisher
from .formatter import _intervals as intervals_text
from .parser import QUEUES
from .reminders import LEAD_OPTIONS
from .storage import Storage

router = Router()


def _keyboard(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows
    ])


def queue_keyboard() -> InlineKeyboardMarkup:
    rows = [[(q, f"my:q:{q}") for q in QUEUES[i:i + 2]] for i in range(0, len(QUEUES), 2)]
    rows.append([("Скасувати", "my:cancel")])
    return _keyboard(rows)


async def settings_view(storage: Storage, user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    queue, lead, notify_back = await storage.get_prefs(user_id)
    by_day = await publisher.latest_by_day(storage)
    today = publisher.kyiv_now().date()
    lines = [f"💡 <b>Моя черга: {queue}</b>", ""]
    for label, day in (("Сьогодні", today), ("Завтра", today + timedelta(days=1))):
        found = by_day.get(day)
        if found:
            lines.append(f"{label}: {intervals_text(found[0].queues.get(queue, []))}")
    lines += [
        "",
        f"⚠️ Нагадувати за <b>{lead} хв</b> до відключення.",
        f"💡 Коли світло має з'явитися: {'повідомляти' if notify_back else 'не повідомляти'}.",
    ]
    rows = [
        [(f"• {m} хв •" if m == lead else f"{m} хв", f"my:lead:{m}") for m in LEAD_OPTIONS],
        [("💡 Світло з'явилось: вимкнути" if notify_back else "💡 Світло з'явилось: увімкнути", "my:back")],
        [("Змінити чергу", "my:change"), ("🔕 Вимкнути нагадування", "my:off")],
    ]
    return "\n".join(lines), _keyboard(rows)


@router.message(Command("my"), F.chat.type == ChatType.PRIVATE)
async def cmd_my(message: Message, storage: Storage, owner_ids: FrozenSet[int]) -> None:
    if message.from_user.id not in owner_ids:
        return
    prefs = await storage.get_prefs(message.from_user.id)
    if not prefs or not prefs[0]:
        await message.answer("Оберіть свою підчергу (дізнатися: cherkasyoblenergo.com/off):",
                             reply_markup=queue_keyboard())
        return
    text, keyboard = await settings_view(storage, message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("my:"))
async def on_my_button(cb: CallbackQuery, storage: Storage, owner_ids: FrozenSet[int]) -> None:
    user_id = cb.from_user.id
    if user_id not in owner_ids:
        await cb.answer("Лише для власника бота", show_alert=True)
        return
    parts = cb.data.split(":")
    action = parts[1]
    note = None
    if action == "cancel":
        await cb.message.edit_text("Скасовано.")
        await cb.answer()
        return
    if action == "off":
        await storage.clear_prefs(user_id)
        await cb.message.edit_text("🔕 Нагадування вимкнено. Увімкнути знову: /my")
        await cb.answer()
        return
    if action == "change":
        await cb.message.edit_text("Оберіть свою підчергу:", reply_markup=queue_keyboard())
        await cb.answer()
        return
    if action == "q" and parts[2] in QUEUES:
        await storage.set_pref(user_id, "queue", parts[2])
        note = f"Черга {parts[2]} збережена"
    elif action == "lead" and parts[2].isdigit() and int(parts[2]) in LEAD_OPTIONS:
        await storage.set_pref(user_id, "lead_minutes", int(parts[2]))
        note = f"Нагадуватиму за {parts[2]} хв"
    elif action == "back":
        prefs = await storage.get_prefs(user_id)
        await storage.set_pref(user_id, "notify_back", not (prefs and prefs[2]))
    prefs = await storage.get_prefs(user_id)
    if not prefs or not prefs[0]:
        await cb.answer()
        return
    text, keyboard = await settings_view(storage, user_id)
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        pass  # нічого не змінилось — Telegram відхиляє однакове редагування
    await cb.answer(note)
