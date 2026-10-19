"""Автопублікація при підключенні каналу і команда /post."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from aiogram.enums import ChatMemberStatus

from bot import handlers, publisher
from bot.storage import Storage
from tests.test_dates import BODY, NOW

OWNERS = frozenset({1})


class FakeBot:
    def __init__(self):
        self.photos, self.messages, self.left = [], [], []

    async def send_photo(self, chat_id, photo, caption, parse_mode):
        self.photos.append((chat_id, caption))
        return SimpleNamespace(message_id=len(self.photos))

    async def pin_chat_message(self, chat_id, message_id, disable_notification=False):
        pass

    async def unpin_chat_message(self, chat_id, message_id=None):
        pass

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))
        return SimpleNamespace(message_id=100 + len(self.messages))

    async def leave_chat(self, chat_id):
        self.left.append(chat_id)


def member_event(chat_id, chat_type, from_id, status=ChatMemberStatus.ADMINISTRATOR):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type, title="Зоя Вовк", full_name=None),
        new_chat_member=SimpleNamespace(status=status),
        from_user=SimpleNamespace(id=from_id),
    )


def run(tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(publisher, "kyiv_now", lambda: NOW)

    async def main():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            for pid, day in ((1, "9"), (2, "10")):
                await storage.save_post(pid, f"Графік ГПВ на {day} жовтня.\n" + BODY, f"https://t.me/x/{pid}",
                                        datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc).isoformat(), True)
            await scenario(storage, FakeBot())
        finally:
            await storage.close()
    asyncio.run(main())


def test_new_channel_gets_current_schedule(tmp_path, monkeypatch):
    async def scenario(storage, bot):
        await handlers.on_my_member(member_event(-100, "channel", 1), storage, bot, OWNERS)
        assert [c for c, _ in bot.photos] == [-100, -100]  # сьогодні + завтра
        assert "9 жовтня" in bot.photos[0][1] and "10 жовтня" in bot.photos[1][1]
        assert bot.messages and "Актуальний графік уже опубліковано" in bot.messages[-1][1]

        # зміна прав у вже підключеному каналі — повторно не публікуємо
        await handlers.on_my_member(member_event(-100, "channel", 1), storage, bot, OWNERS)
        assert len(bot.photos) == 2

    run(tmp_path, monkeypatch, scenario)


def test_channel_added_by_stranger_gets_nothing(tmp_path, monkeypatch):
    async def scenario(storage, bot):
        await handlers.on_my_member(member_event(-100, "channel", 999), storage, bot, OWNERS)
        assert bot.photos == [] and bot.left == [-100]
        assert -100 not in await storage.active_chats()

    run(tmp_path, monkeypatch, scenario)


class FakeCallback:
    def __init__(self, from_id, data):
        self.from_user = SimpleNamespace(id=from_id)
        self.data = data
        self.alerts, self.edited = [], None
        cb = self

        class Msg:
            async def edit_text(self, text, **kw):
                cb.edited = text
        self.message = Msg()

    async def answer(self, text=None, show_alert=False):
        if show_alert:
            self.alerts.append(text)


def test_post_button(tmp_path, monkeypatch):
    async def scenario(storage, bot):
        await storage.add_chat(-100, "Зоя Вовк", "channel", 1)

        stranger = FakeCallback(5, "post:-100")
        await handlers.on_post_button(stranger, storage, bot, OWNERS)
        assert stranger.alerts and bot.photos == []

        cb = FakeCallback(1, "post:-100")
        await handlers.on_post_button(cb, storage, bot, OWNERS)
        assert [c for c, _ in bot.photos] == [-100, -100]
        assert "Опубліковано в <b>Зоя Вовк</b> (графіків: 2)" in cb.edited

    run(tmp_path, monkeypatch, scenario)
