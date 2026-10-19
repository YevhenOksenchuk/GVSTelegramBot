"""Команди бота та реєстрація чатів."""
from __future__ import annotations

from datetime import date, datetime
from html import escape
from typing import FrozenSet, Optional

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import publisher
from .access import is_allowed, request_access, welcome_text
from .config import Config
from .maintenance import LAST_BACKUP_AT, send_backup
from .parser import date_label
from .publisher import Watcher, fix_messages, kyiv_now, publish_announcement, publish_current
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
        # Змінили права в уже підключеному чаті — можливо, дали право закріплювати: спробуємо знову
        await storage.set_pin_failed(chat.id, None)
        if publisher.ALERTS is not None:
            publisher.ALERTS.reset(f"pin:{chat.id}")
        return
    # Новий чат — одразу публікуємо актуальний графік, щоб не чекати наступного посту обленерго
    posted = await publish_current(bot, storage, chat.id, pin=True)
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


def _fmt_time(value: Optional[datetime]) -> str:
    return value.strftime("%d.%m %H:%M:%S") if value else "ще не було"


def _fmt_uptime(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{days} дн"] if days else []
    if hours or days:
        parts.append(f"{hours} год")
    parts.append(f"{minutes} хв")
    return " ".join(parts)


@router.message(Command("stats"))
async def cmd_stats(message: Message, storage: Storage, watcher: Watcher, owner_ids: FrozenSet[int],
                    config: Config, started_at: datetime) -> None:
    if message.from_user.id not in owner_ids:
        return
    rows = await storage.stats()
    by_type = "\n".join(f"• {t}: {n}" for t, n in rows) or "• немає"
    last_backup = await storage.get_meta(LAST_BACKUP_AT) or "ще не було"
    err = f"\n⚠️ Остання помилка: {escape(watcher.last_error)}" if watcher.last_error else ""
    await message.answer(
        f"📊 Активні чати:\n{by_type}\n\n"
        f"Версія: <code>{escape(config.bot_version)}</code>\n"
        f"Запущено: {_fmt_time(started_at)} (аптайм {_fmt_uptime((kyiv_now() - started_at).total_seconds())})\n"
        f"Остання перевірка каналу: {_fmt_time(watcher.last_check)}\n"
        f"Остання вдала перевірка: {_fmt_time(watcher.last_success)}\n"
        f"Останній бекап: {last_backup}{err}",
        parse_mode="HTML",
    )


@router.message(Command("backup"), F.chat.type == ChatType.PRIVATE)
async def cmd_backup(message: Message, storage: Storage, bot: Bot, owner_ids: FrozenSet[int]) -> None:
    """Власник: надіслати копію бази (усім власникам) просто зараз."""
    if message.from_user.id not in owner_ids:
        return
    if not await send_backup(bot, storage, owner_ids):
        await message.answer("⚠️ Не вдалося надіслати бекап — дивіться логи на сервері.")


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
    sent = await publish_current(bot, storage, chat_id, pin=True)
    if sent:
        await cb.message.edit_text(f"✅ Опубліковано в <b>{title}</b> (графіків: {sent})", parse_mode="HTML")
    else:
        await cb.message.edit_text(
            f"⚠️ Не вдалося опублікувати в <b>{title}</b> — немає графіків або бот втратив доступ до чату.",
            parse_mode="HTML",
        )


def _owner_keyboard(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text[:60], callback_data=data) for text, data in row] for row in rows
    ])


async def _pin_keyboard(storage: Storage) -> InlineKeyboardMarkup:
    chats = await storage.connected_chats_pins()
    if not chats:
        return _owner_keyboard([[("Немає підключених каналів чи груп", "pin:none")]])
    return _owner_keyboard([
        [(f"{'✅' if enabled else '❌'} {_KIND.get(type_, '')} {title}", f"pin:{chat_id}")]
        for chat_id, title, type_, enabled in chats
    ])


@router.message(Command("pin"), F.chat.type == ChatType.PRIVATE)
async def cmd_pin(message: Message, storage: Storage, owner_ids: FrozenSet[int]) -> None:
    """Власник: увімкнути/вимкнути закріплення актуального графіка по чатах."""
    if message.from_user.id not in owner_ids:
        return
    await message.answer(
        "📌 Закріплення актуального графіка. Натисніть чат, щоб увімкнути або вимкнути.\n"
        "Для каналу боту потрібне право «Редагувати повідомлення інших», для групи — «Закріплювати повідомлення».",
        reply_markup=await _pin_keyboard(storage),
    )


@router.callback_query(F.data.startswith("pin:"))
async def on_pin_button(cb: CallbackQuery, storage: Storage, owner_ids: FrozenSet[int]) -> None:
    if cb.from_user.id not in owner_ids:
        await cb.answer("Лише для власника бота", show_alert=True)
        return
    target = cb.data.split(":", 1)[1]
    if target == "none":
        await cb.answer()
        return
    chat_id = int(target)
    _, enabled, _ = await storage.pin_state(chat_id)
    await storage.set_pin_enabled(chat_id, not enabled)
    if publisher.ALERTS is not None:
        publisher.ALERTS.reset(f"pin:{chat_id}")
    await cb.message.edit_reply_markup(reply_markup=await _pin_keyboard(storage))
    await cb.answer("Закріплення вимкнено" if enabled else "Закріплення увімкнено")


@router.message(Command("fix"), F.chat.type == ChatType.PRIVATE)
async def cmd_fix(message: Message, storage: Storage, owner_ids: FrozenSet[int]) -> None:
    """Власник: замінити картинку й текст уже надісланих графіків на актуальну версію (після виправлень)."""
    if message.from_user.id not in owner_ids:
        return
    days = await storage.sent_days(3)
    if not days:
        await message.answer("Бот ще нічого не публікував.")
        return
    rows = [[(f"{date_label(date.fromisoformat(day))} — чатів: {n}", f"fix:ask:{day}")] for day, n in days]
    rows.append([("Скасувати", "fix:cancel")])
    await message.answer(
        "🛠 Який графік перевидати? Бот замінить картинку й текст у вже надісланих повідомленнях.",
        reply_markup=_owner_keyboard(rows),
    )


@router.callback_query(F.data.startswith("fix:"))
async def on_fix_button(cb: CallbackQuery, storage: Storage, bot: Bot, owner_ids: FrozenSet[int]) -> None:
    if cb.from_user.id not in owner_ids:
        await cb.answer("Лише для власника бота", show_alert=True)
        return
    parts = cb.data.split(":")
    action = parts[1]
    if action == "cancel":
        await cb.message.edit_text("Скасовано.")
        await cb.answer()
        return
    day = date.fromisoformat(parts[2])
    if action == "ask":
        n = len(await storage.latest_sent_per_chat(day.isoformat()))
        await cb.message.edit_text(
            f"Замінити картинку й текст у {n} повідомленнях за <b>{date_label(day)}</b> "
            "на актуальну версію графіка?\nПідписники сповіщень не отримають.",
            parse_mode="HTML",
            reply_markup=_owner_keyboard([[("✅ Так, замінити", f"fix:go:{day.isoformat()}"),
                                           ("Скасувати", "fix:cancel")]]),
        )
        await cb.answer()
        return
    await cb.answer("Оновлюю…")
    await cb.message.edit_text("⏳ Оновлюю повідомлення…")
    ok, failures = await fix_messages(bot, storage, day)
    lines = [f"✅ Оновлено повідомлень за {date_label(day)}: {ok}"]
    if failures:
        lines.append("⚠️ Не вдалося:")
        lines += [f"• {escape(title)}: {escape(err)}" for title, err in failures[:10]]
        lines.append("Для каналу боту потрібне право «Редагувати повідомлення інших».")
    await cb.message.edit_text("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data.startswith("ann:"))
async def on_announcement_button(cb: CallbackQuery, storage: Storage, bot: Bot, owner_ids: FrozenSet[int]) -> None:
    """Власник вирішує, чи публікувати оголошення обленерго; рішення видно всім власникам."""
    if cb.from_user.id not in owner_ids:
        await cb.answer("Лише для власника бота", show_alert=True)
        return
    _, action, raw_id = cb.data.split(":")
    post_id = int(raw_id)
    if await storage.announcement_status(post_id) != "pending":
        await cb.answer("Уже вирішено", show_alert=True)
        return
    if action == "pub":
        await storage.set_announcement(post_id, "published", cb.from_user.id)  # спершу статус — проти подвійного кліку
        await cb.answer("Публікую…")
        sent = await publish_announcement(bot, storage, post_id)
        result = f"📣 Опубліковано в {sent} чатів"
    else:
        await storage.set_announcement(post_id, "skipped", cb.from_user.id)
        result = "🙈 Пропущено"
    if len(owner_ids) > 1:
        result += f"\n<i>вирішив(ла): {escape(cb.from_user.full_name or str(cb.from_user.id))}</i>"
    row = await storage.get_post(post_id)
    if row:
        result += f"\n{row[1]}"
    edited = False
    for chat_id, message_id in await storage.pop_prompts("announcement", post_id):
        try:
            await bot.edit_message_text(result, chat_id=chat_id, message_id=message_id, parse_mode="HTML",
                                        disable_web_page_preview=True)
            edited = edited or (chat_id == cb.message.chat.id and message_id == cb.message.message_id)
        except Exception:
            pass
    if not edited:
        try:
            await cb.message.edit_text(result, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            pass
    await cb.answer()


async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
