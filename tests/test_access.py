"""Приватний режим: запит доступу, кнопки власника, відкликання."""
import asyncio
from types import SimpleNamespace

from bot import access
from bot.storage import Storage

OWNER = 1
OWNERS = frozenset({OWNER})


class FakeBot:
    def __init__(self, unreachable=()):
        self.messages, self.left, self.edits = [], [], []
        self.unreachable = set(unreachable)

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.unreachable:
            raise RuntimeError("Forbidden: bot can't initiate conversation with a user")
        self.messages.append((chat_id, text, kw.get("reply_markup")))
        return SimpleNamespace(message_id=len(self.messages))

    async def edit_message_text(self, text, chat_id, message_id, **kw):
        self.edits.append((chat_id, message_id, text))

    async def leave_chat(self, chat_id):
        self.left.append(chat_id)

    async def get_me(self):
        return SimpleNamespace(username="test_bot")


class FakeMessage:
    def __init__(self, user_id, name="Зоя Вовк", username="zoya"):
        self.from_user = SimpleNamespace(id=user_id, full_name=name, username=username)
        self.answers = []
        self.edited = None

    async def answer(self, text, **kw):
        self.answers.append(text)

    async def edit_text(self, text, **kw):
        self.edited = text


class FakeCallback:
    def __init__(self, from_id, data):
        self.from_user = SimpleNamespace(id=from_id)
        self.data = data
        self.from_user.full_name = f"Власник {from_id}"
        self.message = FakeMessage(from_id)
        self.message.chat = SimpleNamespace(id=from_id)
        self.message.message_id = 1
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        if text:
            self.alerts.append(text)


def run(tmp_path, scenario, bot=None):
    async def main():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            await scenario(storage, bot or FakeBot())
        finally:
            await storage.close()
    asyncio.run(main())


def test_request_allow_and_revoke(tmp_path):
    async def scenario(storage, bot):
        assert await access.is_allowed(storage, OWNER, OWNERS)
        assert not await access.is_allowed(storage, 42, OWNERS)

        msg = FakeMessage(42)
        await access.request_access(msg, storage, bot, OWNERS)
        assert "Запит надіслано" in msg.answers[-1]
        owner_msg = bot.messages[-1]
        assert owner_msg[0] == OWNER and "Зоя Вовк" in owner_msg[1]
        buttons = [b.callback_data for b in owner_msg[2].inline_keyboard[0]]
        assert buttons == ["acc:allow:42", "acc:deny:42"]

        # повторний /start не спамить власника
        await access.request_access(FakeMessage(42), storage, bot, OWNERS)
        assert len(bot.messages) == 1

        # чужий не може натиснути кнопку
        cb = FakeCallback(42, "acc:allow:42")
        await access.on_access_button(cb, storage, bot, OWNERS)
        assert cb.alerts and not await access.is_allowed(storage, 42, OWNERS)

        cb = FakeCallback(OWNER, "acc:allow:42")
        await access.on_access_button(cb, storage, bot, OWNERS)
        assert await access.is_allowed(storage, 42, OWNERS)
        assert bot.edits == [(OWNER, 1, bot.edits[0][2])] and "Доступ надано" in bot.edits[0][2]
        assert "вирішив" not in bot.edits[0][2]  # один власник — без підпису, хто вирішив
        assert bot.messages[-1][0] == 42 and "Доступ надано" in bot.messages[-1][1]
        assert 42 in await storage.active_chats()  # особиста підписка увімкнена

        # друг додав бота у свою групу
        await storage.add_chat(-500, "Група Зої", "group", 42)
        cb = FakeCallback(OWNER, "acc:revoke:42")
        await access.on_access_button(cb, storage, bot, OWNERS)
        assert not await access.is_allowed(storage, 42, OWNERS)
        assert await storage.active_chats() == []
        assert bot.left == [-500]

    run(tmp_path, scenario)


def test_deny(tmp_path):
    async def scenario(storage, bot):
        await access.request_access(FakeMessage(7), storage, bot, OWNERS)
        await access.on_access_button(FakeCallback(OWNER, "acc:deny:7"), storage, bot, OWNERS)
        msg = FakeMessage(7)
        await access.request_access(msg, storage, bot, OWNERS)
        assert "доступ не надано" in msg.answers[-1]
        assert len(bot.messages) == 1  # повторного запиту власнику немає

    run(tmp_path, scenario)


def test_no_owner_means_open(tmp_path):
    async def scenario(storage, bot):
        assert await access.is_allowed(storage, 123, frozenset())

    run(tmp_path, scenario)


def test_two_owners(tmp_path):
    owners = frozenset({1, 2})

    async def scenario(storage, bot):
        assert await access.is_allowed(storage, 2, owners)
        await access.request_access(FakeMessage(42), storage, bot, owners)
        assert sorted(m[0] for m in bot.messages) == [1, 2]  # запит обом

        cb = FakeCallback(2, "acc:allow:42")  # вирішує другий власник
        cb.message.message_id = 2
        await access.on_access_button(cb, storage, bot, owners)
        assert await access.is_allowed(storage, 42, owners)
        # повідомлення з запитом оновлено в ОБОХ власників
        assert sorted((c, m) for c, m, _ in bot.edits) == [(1, 1), (2, 2)]
        assert all("Доступ надано" in t and "Власник 2" in t for _, _, t in bot.edits)

    run(tmp_path, scenario)


def test_owner_who_never_pressed_start(tmp_path):
    owners = frozenset({1, 2})

    async def scenario(storage, bot):
        msg = FakeMessage(42)
        await access.request_access(msg, storage, bot, owners)
        assert [m[0] for m in bot.messages] == [1]  # другому написати не можна, але першому дійшло
        assert "Запит надіслано" in msg.answers[-1]

    run(tmp_path, scenario, FakeBot(unreachable={2}))


def test_parse_owner_ids():
    from bot.config import parse_owner_ids
    assert parse_owner_ids("111111111,222222222") == {111111111, 222222222}
    assert parse_owner_ids(" 1, 2 ") == {1, 2}
    assert parse_owner_ids("0") == frozenset() and parse_owner_ids("") == frozenset()
