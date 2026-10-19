"""Фаза 3: «Моя черга» і нагадування (лише власники)."""
import asyncio
from datetime import date, datetime
from types import SimpleNamespace

from bot import publisher, reminders
from bot.formatter import format_caption
from bot.myqueue import cmd_my, on_my_button
from bot.parser import parse_post
from bot.publisher import KYIV
from bot.storage import Storage
from tests.test_dates import BODY, NOW

OWNERS = frozenset({1, 2})
POSTED = datetime(2026, 10, 9, 5, 0, tzinfo=KYIV).isoformat()
Q11 = "1.1 05:00 - 07:00, 15:00 - 17:00, 21:00 - 23:00"  # рядок 1.1 у BODY


def at(day, hh, mm=0):
    return datetime(2026, 10, day, hh, mm, tzinfo=KYIV)


def schedule_text(day, q11=Q11, updated=False):
    head = "Оновлено графік ГПВ" if updated else "Графік ГПВ"
    return f"{head} на {day} жовтня.\n" + BODY.replace(Q11, q11)


class FakeBot:
    def __init__(self):
        self.messages, self.photos = [], []

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.messages))

    async def send_photo(self, chat_id, photo, caption, parse_mode):
        self.photos.append((chat_id, caption))
        return SimpleNamespace(message_id=100 + len(self.photos))

    async def pin_chat_message(self, *a, **kw):
        pass

    async def unpin_chat_message(self, *a, **kw):
        pass


class FakeMessage:
    def __init__(self, user_id):
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw.get("reply_markup")))


class FakeCallback:
    def __init__(self, from_id, data):
        self.from_user = SimpleNamespace(id=from_id)
        self.data = data
        self.alerts, self.edited, self.notes = [], None, []
        cb = self

        class Msg:
            async def edit_text(self, text, **kw):
                cb.edited = text

        self.message = Msg()

    async def answer(self, text=None, show_alert=False):
        (self.alerts if show_alert else self.notes).append(text)


def test_absolute_intervals_merges_midnight():
    today = parse_post(schedule_text(9, "1.1 05:00 - 07:00, 23:00 - 24:00"), date(2026, 10, 9))
    tomorrow = parse_post(schedule_text(10, "1.1 00:00 - 01:00, 15:00 - 17:00"), date(2026, 10, 9))
    by_day = {date(2026, 10, 9): (today, "u"), date(2026, 10, 10): (tomorrow, "u")}
    days = (date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10))
    assert reminders.absolute_intervals(by_day, "1.1", days) == [
        (at(9, 5), at(9, 7)), (at(9, 23), at(10, 1)), (at(10, 15), at(10, 17)),
    ]


def test_due_events_windows_and_texts():
    ivs = [(at(9, 15), at(9, 17)), (at(9, 21), at(9, 23))]
    assert reminders.due_events(ivs, at(9, 14, 29), 30, True, []) == []
    ev = reminders.due_events(ivs, at(9, 14, 30), 30, True, [])
    assert [(e.kind, e.at) for e in ev] == [("pre", at(9, 15))]
    assert reminders.format_event("3.2", ev[0], at(9, 14, 30)) == \
        "⚠️ Черга 3.2: о 15:00 вимкнуть світло (до 17:00). Через 30 хв."
    assert reminders.format_event("3.2", ev[0], at(9, 15)).endswith("Вже зараз.")
    assert reminders.due_events(ivs, at(9, 15), 30, True, []) == []  # вже почалось — пізно нагадувати
    ev = reminders.due_events(ivs, at(9, 17, 5), 30, True, [])
    assert [(e.kind, e.at, e.next_start) for e in ev] == [("back", at(9, 17), at(9, 21))]
    assert reminders.format_event("3.2", ev[0], at(9, 17, 5)) == \
        "💡 Черга 3.2: за графіком світло має з'явитися о 17:00. Наступне відключення о 21:00."
    assert reminders.due_events(ivs, at(9, 17, 10), 30, True, []) == []
    assert reminders.due_events(ivs, at(9, 17, 5), 30, False, []) == []  # «світло з'явилось» вимкнено
    ev = reminders.due_events(ivs, at(9, 23, 1), 10, True, [])
    assert reminders.format_event("3.2", ev[0], at(9, 23, 1)).endswith("Далі без відключень за відомим графіком.")
    # попередили про 21:00, а потім інтервал зник з графіка
    ev = reminders.due_events([ivs[0]], at(9, 20, 40), 30, True, [at(9, 21)])
    assert [(e.kind, e.at) for e in ev] == [("cancel", at(9, 21))]
    assert reminders.format_event("3.2", ev[0], at(9, 20, 40)) == \
        "✅ Черга 3.2: відключення о 21:00 прибрали з графіка."
    assert reminders.due_events([ivs[0]], at(9, 21, 5), 30, True, [at(9, 21)]) == []  # уже минуло — мовчимо
    # завтра, через північ
    ev = reminders.due_events([(at(10, 0, 10), at(10, 1))], at(9, 23, 45), 30, True, [])
    assert reminders.format_event("1.1", ev[0], at(9, 23, 45)) == \
        "⚠️ Черга 1.1: о 00:10 завтра вимкнуть світло (до 01:00). Через 25 хв."
    ev = reminders.due_events([(at(9, 23), at(10, 1))], at(9, 22, 50), 10, True, [])
    assert "(до 01:00 завтра)" in reminders.format_event("1.1", ev[0], at(9, 22, 50))


def test_run_once_dedupes_cancels_and_crosses_midnight(tmp_path):
    async def scenario():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            await storage.save_post(10, schedule_text(9), "u10", POSTED, True)
            await storage.set_pref(1, "queue", "1.1")  # власник
            await storage.set_pref(1, "lead_minutes", 20)
            await storage.set_pref(42, "queue", "1.1")  # не власник — нагадувань не отримує
            bot = FakeBot()
            assert await reminders.run_once(bot, storage, OWNERS, at(9, 14, 39)) == 0
            assert await reminders.run_once(bot, storage, OWNERS, at(9, 14, 40)) == 1
            assert bot.messages == [(1, "⚠️ Черга 1.1: о 15:00 вимкнуть світло (до 17:00). Через 20 хв.")]
            assert await reminders.run_once(bot, storage, OWNERS, at(9, 14, 50)) == 0  # без повторів
            assert await reminders.run_once(bot, storage, OWNERS, at(9, 17, 3)) == 1
            assert bot.messages[-1][1].startswith("💡 Черга 1.1: за графіком світло має з'явитися о 17:00.")

            # попередили про 21:00, обленерго прибрало цей інтервал — приходить скасування
            assert await reminders.run_once(bot, storage, OWNERS, at(9, 20, 45)) == 1
            assert "о 21:00 вимкнуть" in bot.messages[-1][1]
            await storage.save_post(11, schedule_text(9, "1.1 05:00 - 07:00, 15:00 - 17:00", updated=True),
                                    "u11", at(9, 20, 46).isoformat(), True)
            assert await reminders.run_once(bot, storage, OWNERS, at(9, 20, 47)) == 1
            assert bot.messages[-1] == (1, "✅ Черга 1.1: відключення о 21:00 прибрали з графіка.")
            assert await reminders.run_once(bot, storage, OWNERS, at(9, 20, 48)) == 0

            # завтра о 00:10: нагадування до півночі, після півночі не дублюється
            await storage.save_post(12, schedule_text(10, "1.1 00:10 - 01:00"), "u12", at(9, 21).isoformat(), True)
            assert await reminders.run_once(bot, storage, OWNERS, at(9, 23, 55)) == 1
            assert "о 00:10 завтра вимкнуть" in bot.messages[-1][1]
            assert await reminders.run_once(bot, storage, OWNERS, at(10, 0, 2)) == 0
            assert all(chat_id == 1 for chat_id, _ in bot.messages)
        finally:
            await storage.close()

    asyncio.run(scenario())


def test_my_queue_buttons(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "kyiv_now", lambda: NOW)

    async def scenario():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            await storage.save_post(10, schedule_text(9), "u10", POSTED, True)
            stranger = FakeMessage(99)
            await cmd_my(stranger, storage, OWNERS)
            assert stranger.answers == []
            msg = FakeMessage(1)
            await cmd_my(msg, storage, OWNERS)
            assert "Оберіть свою підчергу" in msg.answers[-1][0]
            assert msg.answers[-1][1].inline_keyboard[0][0].callback_data == "my:q:1.1"

            cb = FakeCallback(1, "my:q:3.2")
            await on_my_button(cb, storage, OWNERS)
            assert "Моя черга: 3.2" in cb.edited and "за <b>30 хв</b>" in cb.edited
            assert "Сьогодні: 01:00–03:00 · 12:00–14:00 · 19:00–21:00" in cb.edited
            cb = FakeCallback(1, "my:lead:10")
            await on_my_button(cb, storage, OWNERS)
            assert "за <b>10 хв</b>" in cb.edited and await storage.get_prefs(1) == ("3.2", 10, True)
            cb = FakeCallback(1, "my:back")
            await on_my_button(cb, storage, OWNERS)
            assert await storage.get_prefs(1) == ("3.2", 10, False) and "не повідомляти" in cb.edited
            cb = FakeCallback(1, "my:lead:7")  # не з переліку — ігноруємо
            await on_my_button(cb, storage, OWNERS)
            assert await storage.get_prefs(1) == ("3.2", 10, False)
            msg = FakeMessage(1)
            await cmd_my(msg, storage, OWNERS)
            assert "Моя черга: 3.2" in msg.answers[-1][0]
            cb = FakeCallback(1, "my:off")
            await on_my_button(cb, storage, OWNERS)
            assert await storage.get_prefs(1) is None and "вимкнено" in cb.edited
            stranger = FakeCallback(99, "my:q:1.1")
            await on_my_button(stranger, storage, OWNERS)
            assert stranger.alerts and await storage.get_prefs(99) is None
        finally:
            await storage.close()

    asyncio.run(scenario())


def test_personal_line_in_caption_and_broadcast(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "kyiv_now", lambda: NOW)
    s = parse_post(schedule_text(9), date(2026, 10, 9))
    cap = format_caption(s, "u", my_queue="3.2")
    assert "💡 <b>Ваша 3.2:</b> 01:00–03:00 · 12:00–14:00 · 19:00–21:00" in cap
    assert cap.index("Ваша 3.2") < cap.index("<b>1.1</b>")
    assert "Ваша" not in format_caption(s, "u")

    async def scenario():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            await storage.add_chat(-100, "Канал", "channel", 1)
            await storage.add_chat(1, "Власник", "private", 1)
            await storage.set_pref(1, "queue", "3.2")
            s.post_id = 10
            bot = FakeBot()
            await publisher.broadcast(bot, storage, s, "u")
            caps = dict(bot.photos)
            assert "Ваша 3.2" in caps[1] and "Ваша 3.2" not in caps[-100]
            await storage.save_post(10, schedule_text(9), "u10", POSTED, True)
            bot = FakeBot()
            await publisher.publish_current(bot, storage, 1)  # /now в особистих
            assert "Ваша 3.2" in bot.photos[0][1]
        finally:
            await storage.close()

    asyncio.run(scenario())
