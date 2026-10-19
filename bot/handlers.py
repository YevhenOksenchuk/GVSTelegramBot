"""Команди бота та реєстрація чатів."""
from __future__ import annotations

from html import escape
from typing import FrozenSet

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .access import is_allowed, request_access, welcome_text
from .publisher import Watcher, publish_current
from .storage import Storage

router = Router()

ACTIVE_STATUSES = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}


def _title(chat) -> str:
    return chat.title or chat.full_name or str(chat.id)


@router.my_chat_member()
async def on_my_member(event: ChatMemberUpdated, storage: Storage, bot: Bot, owner_ids: FrozenSet[int]) -> None:
    """Бота додали/видалили з каналу, групи або заблокували в особистих."""
    chat, status = event.chat, event.new_chat_member.status
    if status not in ACTIVE_STATUSES:
        await storage.deactivate_chat(chat.id)
        return
    if chat.type == ChatType.PRIVATE:
        return  # особисті підписки — через /start
    if chat.type == ChatType.CHANNEL and status != ChatMemberStatus.ADMINISTRATOR:
        return
    if not await is_allowed(storage, event.from_user.id, owner_ids):
        # Бот приватний: додавати його можуть лише ті, кому власник дозволив
        try:
            await bot.leave_chat(chat.id)
        except Exception:
            pass
        try:
            await bot.send_message(
                event.from_user.id,
                "🔒 Бот приватний — додавати його в канали й групи можуть лише користувачі з доступом. "
                "Надішліть мені /start, щоб попросити доступ.",
            )
        except Exception:
            pass
        return
    was_active = await storage.is_active(chat.id)
    await storage.add_chat(chat.id, _title(chat), chat.type, event.from_user.id)
    if was_active:
        return  # змінили права в уже підключеному чаті — нічого не робимо
    # Новий чат — одразу публікуємо актуальний графік, щоб не чекати наступного посту обленерго
    posted = await publish_current(bot, storage, chat.id)
    note = " Актуальний графік уже опубліковано." if posted else ""
    try:
        await bot.send_message(
            event.from_user.id,
            f"✅ Підключено: <b>{escape(_title(chat))}</b>. Нові графіки ГПВ з'являтимуться там автоматично.{note}",
            parse_mode="HTML",
        )
    except Exception:
        pass  # користувач ще не писав боту в особисті


@router.message(F.migrate_to_chat_id)
async def on_migrate(message: Message, storage: Storage) -> None:
    await storage.migrate_chat(message.chat.id, message.migrate_to_chat_id)


@router.message(CommandStart())
async def cmd_start(message: Message, storage: Storage, bot: Bot, owner_ids: FrozenSet[int]) -> None:
    chat, user = message.chat, message.from_user
    allowed = await is_allowed(storage, user.id, owner_ids)
    if chat.type == ChatType.PRIVATE and not allowed:
        await request_access(message, storage, bot, owner_ids)
        return
    if chat.type != ChatType.PRIVATE:
        if not await _is_admin(bot, chat.id, user.id):
            return
        if not allowed:
            await message.answer("🔒 Бот приватний. Напишіть мені в особисті /start, щоб попросити доступ.")
            return
    await storage.add_chat(chat.id, _title(chat), chat.type, user.id)
    if chat.type != ChatType.PRIVATE:
        await message.answer("✅ Графіки ГПВ надходитимуть у цей чат. Вимкнути: /stop")
        return
    await message.answer(await welcome_text(bot), parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("stop"))
async def cmd_stop(message: Message, storage: Storage, bot: Bot) -> None:
    chat = message.chat
    if chat.type != ChatType.PRIVATE and not await _is_admin(bot, chat.id, message.from_user.id):
        return
    await storage.deactivate_chat(chat.id)
    await message.answer("🔕 Відписано. Повернутися: /start")


@router.message(Command("now"))
async def cmd_now(message: Message, storage: Storage, bot: Bot, owner_ids: FrozenSet[int]) -> None:
    # У підключених групах /now може написати будь-хто з учасників; в особистих — лише з доступом
    if not (await storage.is_active(message.chat.id) and message.chat.type != ChatType.PRIVATE) \
            and not await is_allowed(storage, message.from_user.id, owner_ids):
        await message.answer("🔒 Бот приватний. Надішліть /start, щоб попросити доступ.")
        return
    if not await publish_current(bot, storage, message.chat.id):  # сьогодні, потім завтра
        await message.answer("Поки що графіків не знайдено 🤷")


@router.message(Command("stats"))
async def cmd_stats(message: Message, storage: Storage, watcher: Watcher, owner_ids: FrozenSet[int]) -> None:
    if message.from_user.id not in owner_ids:
        return
    rows = await storage.stats()
    by_type = "\n".join(f"• {t}: {n}" for t, n in rows) or "• немає"
    last = watcher.last_check.strftime("%d.%m %H:%M:%S") if watcher.last_check else "ще не було"
    err = f"\n⚠️ Остання помилка: {watcher.last_error}" if watcher.last_error else ""
    await message.answer(f"📊 Активні чати:\n{by_type}\n\nОстання перевірка: {last}{err}")


_KIND = {"channel": "📢", "group": "👥", "supergroup": "👥"}


@router.message(Command("post"), F.chat.type == ChatType.PRIVATE)
async def cmd_post(message: Message, storage: Storage, owner_ids: FrozenSet[int]) -> None:
    """Власник: опублікувати актуальний графік у вибраний канал чи групу."""
    if message.from_user.id not in owner_ids:
        return
    chats = await storage.connected_chats()
    if not chats:
        await message.answer("Немає підключених каналів чи груп.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{_KIND.get(type_, '')} {title}"[:60], callback_data=f"post:{chat_id}")]
        for chat_id, title, type_ in chats
    ] + [[InlineKeyboardButton(text="Скасувати", callback_data="post:cancel")]])
    await message.answer("Куди опублікувати актуальний графік?", reply_markup=keyboard)


@router.callback_query(F.data.startswith("post:"))
async def on_post_button(cb: CallbackQuery, storage: Storage, bot: Bot, owner_ids: FrozenSet[int]) -> None:
    if cb.from_user.id not in owner_ids:
        await cb.answer("Лише для власника бота", show_alert=True)
        return
    target = cb.data.split(":", 1)[1]
    if target == "cancel":
        await cb.message.edit_text("Скасовано.")
        await cb.answer()
        return
    chat_id = int(target)
    title = escape(await storage.get_chat_title(chat_id) or str(chat_id))
    await cb.answer("Публікую…")
    sent = await publish_current(bot, storage, chat_id)
    if sent:
        await cb.message.edit_text(f"✅ Опубліковано в <b>{title}</b> (графіків: {sent})", parse_mode="HTML")
    else:
        await cb.message.edit_text(
            f"⚠️ Не вдалося опублікувати в <b>{title}</b> — немає графіків або бот втратив доступ до чату.",
            parse_mode="HTML",
        )


async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
