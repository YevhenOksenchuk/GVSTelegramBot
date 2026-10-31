"""/month: підсумок за місяць, навігація, «за весь час», публікація."""
import asyncio
from datetime import date, datetime
from types import SimpleNamespace

from bot import publisher
from bot.monthly import ALL, cmd_month, keyboard_for, on_month_button
from bot.publisher import KYIV
from bot.stats import all_time_stats, format_summary, render_month_stats
from bot.storage import Storage
from tests.test_reminders import schedule_text
from tests.test_stats import sched

OWNERS = frozenset({1, 2})


class FakeBot:
    def __init__(self):
        self.photos, self.media, self.markups = [], [], []
        self._n = 0

    def _msg(self, chat_id):
        self._n += 1
        return SimpleNamespace(message_id=self._n, chat=SimpleNamespace(id=chat_id))

    async def send_photo(self, chat_id, photo, caption, parse_mode, **kw):
        self.photos.append((chat_id, caption, kw.get("reply_markup")))
        return self._msg(chat_id)

    async def edit_message_media(self, media, chat_id, message_id, reply_markup=None):
        self.media.append((chat_id, message_id, media.caption, reply_markup))

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.markups.append((chat_id, message_id, reply_markup))


class FakeMessage:
    def __init__(self, user_id, bot):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id)
        self.bot, self.answers = bot, []

    async def answer(self, text, **kw):
        self.answers.append(text)

    async def answer_photo(self, photo, caption, parse_mode, reply_markup=None):
        return await self.bot.send_photo(self.chat.id, photo, caption, parse_mode, reply_markup=reply_markup)


class FakeCallback:
    def __init__(self, from_id, data, message_id=7):
        self.from_user = SimpleNamespace(id=from_id)
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=from_id), message_id=message_id)
        self.alerts, self.notes = [], []

    async def answer(self, text=None, show_alert=False):
        (self.alerts if show_alert else self.notes).append(text)


def texts(markup):
    return [[b.text for b in row] for row in markup.inline_keyboard]


def test_keyboard_navigation_limits():
    kb = keyboard_for("2026-10", date(2026, 10, 7), date(2026, 10, 31))  # дані лише з жовтня
    assert texts(kb) == [["За весь час"], ["📣 Опублікувати"]]
    kb = keyboard_for("2026-10", date(2026, 9, 25), date(2026, 11, 2))  # є вересень і вже листопад
    assert texts(kb) == [["← Вересень", "Листопад →"], ["За весь час"], ["📣 Опублікувати"]]
    assert texts(keyboard_for(ALL, date(2026, 9, 25), date(2026, 11, 2))) == [["Поточний місяць"], ["📣 Опублікувати"]]


def test_all_time_stats_and_summary():
    schedules = [sched(date(2026, 10, d), schedule_text(d), datetime(2026, 10, d - 1, 20, 0, tzinfo=KYIV))
                 for d in (8, 9)]
    st = all_time_stats(schedules, date(2026, 11, 2), data_start=date(2026, 9, 25))
    assert (st.is_month, st.first_day, st.last_day, st.days_covered) == (False, date(2026, 9, 25), date(2026, 11, 2), 39)
    assert st.title == "Без світла за весь час" and st.range_label == "25 вересня – 2 листопада"
    summary = format_summary(st)
    assert "Без світла за весь час" in summary and "Днів з відключеннями: 2 з 39" in summary
    assert render_month_stats(st)[:8] == b"\x89PNG\r\n\x1a\n"
    assert all_time_stats([], date(2026, 11, 2)) is None


def test_month_command_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "kyiv_now", lambda: datetime(2026, 10, 9, 12, 0, tzinfo=KYIV))

    async def scenario():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            bot = FakeBot()
            msg = FakeMessage(1, bot)
            await cmd_month(msg, storage, OWNERS)
            assert msg.answers == ["Ще немає жодного графіка — статистика з'явиться після першого."]

            await storage.save_post(90, "Новини", "u90", datetime(2026, 9, 25, 9, 0, tzinfo=KYIV).isoformat(), False)
            await storage.save_post(1, schedule_text(8), "u1", datetime(2026, 10, 7, 20, 0, tzinfo=KYIV).isoformat(), True)
            await storage.save_post(2, schedule_text(9), "u2", datetime(2026, 10, 8, 21, 0, tzinfo=KYIV).isoformat(), True)
            await storage.add_chat(-100, "Канал", "channel", 1)

            stranger = FakeMessage(99, bot)
            await cmd_month(stranger, storage, OWNERS)
            assert stranger.answers == [] and bot.photos == []

            await cmd_month(FakeMessage(1, bot), storage, OWNERS)
            chat_id, caption, markup = bot.photos[-1]
            assert chat_id == 1 and "Без світла у жовтні" in caption and "(1–9 жовтня)" in caption
            assert texts(markup) == [["← Вересень"], ["За весь час"], ["📣 Опублікувати"]]

            cb = FakeCallback(1, f"mon:view:{ALL}")
            await on_month_button(cb, storage, bot, OWNERS)
            assert "Без світла за весь час" in bot.media[-1][2] and texts(bot.media[-1][3])[0] == ["Поточний місяць"]

            cb = FakeCallback(1, "mon:view:2026-09")
            await on_month_button(cb, storage, bot, OWNERS)
            assert "Без світла у вересні" in bot.media[-1][2] and "(25–30 вересня)" in bot.media[-1][2]

            cb = FakeCallback(1, "mon:view:2026-08")  # до початку даних
            await on_month_button(cb, storage, bot, OWNERS)
            assert cb.alerts == ["Немає даних за цей період"]

            cb = FakeCallback(1, "mon:pub:2026-10")
            await on_month_button(cb, storage, bot, OWNERS)
            assert texts(bot.markups[-1][2]) == [["📢 Канал"], ["← Назад"]]

            cb = FakeCallback(1, "mon:to:-100:2026-10")
            await on_month_button(cb, storage, bot, OWNERS)
            assert bot.photos[-1][0] == -100 and "Без світла у жовтні" in bot.photos[-1][1]
            assert cb.notes == ["✅ Опубліковано в Канал"]
            assert texts(bot.markups[-1][2]) == [["← Вересень"], ["За весь час"], ["📣 Опублікувати"]]

            stranger_cb = FakeCallback(99, "mon:to:-100:2026-10")
            await on_month_button(stranger_cb, storage, bot, OWNERS)
            assert stranger_cb.alerts and len(bot.photos) == 2
        finally:
            await storage.close()

    asyncio.run(scenario())
