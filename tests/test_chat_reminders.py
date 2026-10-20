"""Нагадування в каналах і групах: усі черги, що вимикаються в один час, — одним повідомленням."""
import asyncio
from datetime import date

from bot import reminders
from bot.handlers import on_remind_button
from bot.parser import parse_post
from bot.storage import Storage
from tests.test_reminders import POSTED, FakeBot, FakeCallback, at, schedule_text

OWNERS = frozenset({1, 2})
DAYS = (date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10))


def test_chat_due_groups_and_format():
    by_day = {date(2026, 10, 9): (parse_post(schedule_text(9), date(2026, 10, 9)), "u")}
    groups = reminders.chat_due_groups(by_day, DAYS, at(9, 8, 50), 10)
    assert [(s, [q for q, _ in items]) for s, items in groups] == [(at(9, 9), ["2.1", "5.1"])]
    assert reminders.chat_due_groups(by_day, DAYS, at(9, 9), 10) == []  # уже почалось
    text = reminders.format_chat_reminder(at(9, 9), groups[0][1], at(9, 8, 50), "https://t.me/zoia/5")
    assert text == ("💡 <b>Черги 2.1, 5.1</b> — без світла через 10 хв\n\n"
                    "Електроенергії не буде з 09:00 до 11:00\n\n"
                    '📋 <a href="https://t.me/zoia/5">Переглянути актуальний графік</a>')
    # різний кінець у різних черг
    items = [("1.2", at(9, 12)), ("3.1", at(9, 12)), ("4.1", at(9, 12, 30))]
    assert reminders.format_chat_reminder(at(9, 10), items, at(9, 9, 50), None) == (
        "💡 <b>Черги 1.2, 3.1, 4.1</b> — без світла через 10 хв\n\n"
        "Електроенергії не буде з 10:00:\n• 1.2, 3.1 — до 12:00\n• 4.1 — до 12:30")
    text = reminders.format_chat_reminder(at(9, 23), [("6.2", at(10, 0))], at(9, 22, 45), None)
    assert text == "💡 <b>Черга 6.2</b> — без світла через 15 хв\n\nЕлектроенергії не буде з 23:00 до 24:00"


def test_message_link():
    assert reminders.message_link(-1001234567890, "channel", "zoia", 7) == "https://t.me/zoia/7"
    assert reminders.message_link(-1001234567890, "supergroup", None, 7) == "https://t.me/c/1234567890/7"
    assert reminders.message_link(-5269217149, "group", None, 7) is None  # у звичайній групі посилань немає
    assert reminders.message_link(-1001234567890, "channel", "zoia", None) is None


def test_run_chat_reminders_once(tmp_path):
    async def scenario():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            await storage.save_post(10, schedule_text(9), "u10", POSTED, True)
            await storage.add_chat(-1001, "Канал", "channel", 1, "zoia")
            await storage.add_chat(-1002, "Група без нагадувань", "supergroup", 1)
            await storage.set_remind_lead(-1001, 10)
            await storage.record_sent(-1001, 55, 10, "2026-10-09", "photo")
            bot = FakeBot()
            assert await reminders.run_chat_reminders_once(bot, storage, at(9, 8, 49)) == 0
            assert await reminders.run_chat_reminders_once(bot, storage, at(9, 8, 50)) == 1
            assert bot.messages == [(-1001, "💡 <b>Черги 2.1, 5.1</b> — без світла через 10 хв\n\n"
                                            "Електроенергії не буде з 09:00 до 11:00\n\n"
                                            '📋 <a href="https://t.me/zoia/55">Переглянути актуальний графік</a>')]
            assert await reminders.run_chat_reminders_once(bot, storage, at(9, 8, 55)) == 0  # без повторів
            # оновлення додало 3.1 на 09:00 — досилаємо лише про неї
            updated = schedule_text(9, updated=True).replace(
                "3.1 03:00 - 05:00, 13:00 - 15:00", "3.1 03:00 - 05:00, 09:00 - 10:00, 13:00 - 15:00")
            await storage.save_post(11, updated, "u11", at(9, 8, 56).isoformat(), True)
            assert await reminders.run_chat_reminders_once(bot, storage, at(9, 8, 57)) == 1
            assert bot.messages[-1][1].startswith("💡 <b>Черга 3.1</b> — без світла через 3 хв\n\n"
                                                  "Електроенергії не буде з 09:00 до 10:00")
            assert all(chat_id == -1001 for chat_id, _ in bot.messages)  # у групі без /remind — тиша
            assert bot.silent == [False, False]
            # «комендантська година» 00:00–05:00: нагадування без звуку
            assert await reminders.run_chat_reminders_once(bot, storage, at(9, 2, 50)) == 1  # 3.1 о 03:00
            assert bot.messages[-1][1].startswith("💡 <b>Черга 3.1</b> — без світла через 10 хв") and bot.silent[-1]
            assert reminders.is_quiet(at(9, 4, 59)) and not reminders.is_quiet(at(9, 5)) and not reminders.is_quiet(at(9, 23, 59))
        finally:
            await storage.close()

    asyncio.run(scenario())


def test_remind_buttons(tmp_path):
    async def scenario():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            await storage.add_chat(-1001, "Канал", "channel", 1, "zoia")
            cb = FakeCallback(1, "rem:pick:-1001")
            await on_remind_button(cb, storage, OWNERS)
            assert "за скільки хвилин" in cb.edited
            cb = FakeCallback(1, "rem:set:-1001:15")
            await on_remind_button(cb, storage, OWNERS)
            assert await storage.chats_with_reminders() == [(-1001, "channel", "zoia", 15)]
            assert cb.notes == ["Канал: нагадування за 15 хв"]
            cb = FakeCallback(1, "rem:set:-1001:7")  # не з переліку — ігноруємо
            await on_remind_button(cb, storage, OWNERS)
            assert (await storage.chats_with_reminders())[0][3] == 15
            cb = FakeCallback(1, "rem:set:-1001:0")
            await on_remind_button(cb, storage, OWNERS)
            assert await storage.chats_with_reminders() == [] and cb.notes == ["Канал: нагадування вимкнено"]
            stranger = FakeCallback(99, "rem:set:-1001:10")
            await on_remind_button(stranger, storage, OWNERS)
            assert stranger.alerts and await storage.chats_with_reminders() == []
        finally:
            await storage.close()

    asyncio.run(scenario())
