"""Приватний режим: бот працює лише для користувачів, яким власник (або один із власників) дозволив доступ."""
from __future__ import annotations

import logging
from html import escape
from typing import FrozenSet, Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .storage import Storage

log = logging.getLogger(__name__)
router = Router()

ALLOWED, PENDING, DENIED = "allowed", "pending", "denied"


async def is_allowed(storage: Storage, user_id: int, owner_ids: FrozenSet[int]) -> bool:
    if not owner_ids:  # власників не задано — приватний режим вимкнено
        return True
    return user_id in owner_ids or await storage.user_status(user_id) == ALLOWED


def who(user_id: int, name: Optional[str], username: Optional[str]) -> str:
    label = escape(name or str(user_id))
    return f"{label} (@{escape(username)})" if username else f'<a href="tg://user?id={user_id}">{label}</a>'


def _buttons(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows
    ])


async def welcome_text(bot: Bot) -> str:
    me = await bot.get_me()
    return (
        "👋 Я публікую графіки відключень світла Черкасиобленерго у зручному вигляді.\n\n"
        "✅ Ви підписані — нові графіки приходитимуть сюди.\n\n"
        "<b>Додати мене до свого каналу чи групи:</b>\n"
        f'• <a href="https://t.me/{me.username}?startchannel=true&admin=post_messages">у канал</a> '
        "(як адміністратора з правом публікувати)\n"
        f'• <a href="https://t.me/{me.username}?startgroup=true">у групу</a>\n\n'
        "Команди:\n/now — графік на сьогодні (і на завтра, якщо вже є)\n/stop — відписатися"
    )


async def request_access(message: Message, storage: Storage, bot: Bot, owner_ids: FrozenSet[int]) -> None:
    """Користувач без доступу написав боту — надсилаємо власнику запит із кнопками."""
    user = message.from_user
    status = await storage.user_status(user.id)
    if status == DENIED:
        await message.answer("🔒 Бот приватний, доступ не надано.")
        return
    if status == PENDING:
        await message.answer("⏳ Запит уже надіслано власнику, зачекайте на підтвердження.")
        return
    await storage.set_user(user.id, PENDING, user.full_name, user.username)
    for owner in owner_ids:
        try:
            sent = await bot.send_message(
                owner,
                f"🔔 <b>Запит на доступ</b>\n{who(user.id, user.full_name, user.username)}",
                parse_mode="HTML",
                reply_markup=_buttons([("✅ Дозволити", f"acc:allow:{user.id}"),
                                       ("❌ Відхилити", f"acc:deny:{user.id}")]),
            )
            await storage.save_request_message(user.id, owner, sent.message_id)
        except Exception as e:  # власник ще не натискав Start у боті
            log.warning("Не вдалося надіслати запит власнику %s: %s", owner, e)
    await message.answer("🔒 Бот приватний. Запит надіслано власнику — щойно він підтвердить, я напишу.")


async def revoke(storage: Storage, bot: Bot, user_id: int) -> int:
    """Забрати доступ: вимкнути особисту підписку й вийти з чатів, куди користувач додав бота."""
    await storage.set_user(user_id, DENIED)
    chats = await storage.chats_added_by(user_id)
    for chat_id in chats:
        await storage.deactivate_chat(chat_id)
        if chat_id != user_id:
            try:
                await bot.leave_chat(chat_id)
            except Exception as e:
                log.info("Не вдалося вийти з %s: %s", chat_id, e)
    return len(chats)


@router.callback_query(F.data.startswith("acc:"))
async def on_access_button(cb: CallbackQuery, storage: Storage, bot: Bot, owner_ids: FrozenSet[int]) -> None:
    if cb.from_user.id not in owner_ids:
        await cb.answer("Лише для власника бота", show_alert=True)
        return
    _, action, raw_id = cb.data.split(":")
    user_id = int(raw_id)
    name, username, _ = await storage.get_user(user_id) or (None, None, None)
    person = who(user_id, name, username)

    if action == "allow":
        await storage.set_user(user_id, ALLOWED)
        await storage.add_chat(user_id, name or str(user_id), "private", user_id)
        result = f"✅ Доступ надано: {person}"
        try:
            await bot.send_message(user_id, "✅ Доступ надано!\n\n" + await welcome_text(bot),
                                   parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            result += "\n(не вдалося написати користувачу — хай натисне /start)"
    elif action == "deny":
        await storage.set_user(user_id, DENIED)
        result = f"❌ Відхилено: {person}"
    elif action == "revoke":
        left = await revoke(storage, bot, user_id)
        result = f"🚫 Доступ забрано: {person}" + (f"\nВимкнено чатів: {left}" if left else "")
    else:
        await cb.answer()
        return
    if len(owner_ids) > 1:
        result += f"\n<i>вирішив(ла): {escape(cb.from_user.full_name or str(cb.from_user.id))}</i>"
    # оновити запит у всіх власників, щоб ніхто не натискав повторно
    edited = False
    for chat_id, message_id in await storage.pop_request_messages(user_id):
        try:
            await bot.edit_message_text(result, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
            edited = edited or (chat_id == cb.message.chat.id and message_id == cb.message.message_id)
        except Exception:
            pass  # повідомлення вже змінене або видалене
    if not edited:
        await cb.message.edit_text(result, parse_mode="HTML")
    await cb.answer()


@router.message(Command("users"))
async def cmd_users(message: Message, storage: Storage, owner_ids: FrozenSet[int]) -> None:
    if message.from_user.id not in owner_ids:
        return
    users = await storage.list_users()
    if not users:
        await message.answer("Поки що ніхто не просив доступ.")
        return
    titles = {PENDING: "⏳ Чекають", ALLOWED: "✅ Мають доступ", DENIED: "❌ Відхилені"}
    lines, rows = [], []
    for status in (PENDING, ALLOWED, DENIED):
        group = [u for u in users if u[3] == status]
        if not group:
            continue
        lines.append(f"<b>{titles[status]}:</b>")
        for user_id, name, username, _ in group:
            lines.append("• " + who(user_id, name, username))
            short = (name or str(user_id))[:20]
            if status == PENDING:
                rows.append([(f"✅ {short}", f"acc:allow:{user_id}"), (f"❌ {short}", f"acc:deny:{user_id}")])
            elif status == ALLOWED:
                rows.append([(f"🚫 Забрати: {short}", f"acc:revoke:{user_id}")])
            else:
                rows.append([(f"✅ Дозволити: {short}", f"acc:allow:{user_id}")])
        lines.append("")
    await message.answer("\n".join(lines).strip(), parse_mode="HTML",
                         reply_markup=_buttons(*rows), disable_web_page_preview=True)
