"""Фаза 2: пам'ять надісланих повідомлень, закріплення, /fix, підказка про чергу, оголошення з модерацією."""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest

from bot import publisher
from bot.announcements import format_announcement, is_announcement
from bot.formatter import format_caption
from bot.handlers import on_announcement_button, on_fix_button, on_pin_button
from bot.parser import parse_post
from bot.scraper import Post
from bot.storage import Storage
from tests.test_dates import BODY, NOW

OWNERS = frozenset({1, 2})
UTC = timezone.utc
POSTED = datetime(2026, 10, 9, 5, 0, tzinfo=UTC).isoformat()

GOP = ('Відповідно до команди НЕК "Укренерго" для промисловості та бізнесу Черкаської області 10 жовтня '
       "з 00:00 до 24:00 будуть застосовані графіки обмеження потужності (ГОП).")
NEWS = ("💡Чому в одних регіонах світла менше, ніж в інших? Пояснюємо, від чого це залежить. "
        "Енергетична інфраструктура - головна мішень російських ударів.")
METERS = "ℹ️ Нагадуємо, що 3 жовтня закінчується період передачі показників електролічильників за вересень."
GENERIC_EMERGENCY = "СТАН ЕНЕРГОСИСТЕМИ: у кількох областях України застосовуються аварійні відключення."
CANCEL = "Увага! За командою НЕК «Укренерго» графіки погодинних відключень (ГПВ) на 12 жовтня скасовано."
GAV = "У Черкаській області застосовано графіки аварійних відключень (ГАВ). Час відновлення невідомий."
NO_OUTAGES = "Завтра, 13 жовтня, графіки відключень у Черкаській області не застосовуватимуться."
LOCAL_EMERGENCY = "Через аварійну ситуацію на підстанції частина Черкас без світла. Енергетики працюють."


class FakeBot:
    def __init__(self, pin_fails=(), edit_fails=()):
        self.photos, self.texts, self.pins, self.unpins, self.edits = [], [], [], [], []
        self.pin_fails, self.edit_fails = set(pin_fails), set(edit_fails)
        self._n = 0

    def _msg(self, chat_id):
        self._n += 1
        return SimpleNamespace(message_id=self._n, chat=SimpleNamespace(id=chat_id))

    async def send_photo(self, chat_id, photo, caption, parse_mode):
        self.photos.append((chat_id, caption))
        return self._msg(chat_id)

    async def send_message(self, chat_id, text, **kw):
        self.texts.append((chat_id, text, kw.get("reply_markup")))
        return self._msg(chat_id)

    async def pin_chat_message(self, chat_id, message_id, disable_notification=False):
        if chat_id in self.pin_fails:
            raise TelegramBadRequest(method=None, message="Bad Request: not enough rights to manage pinned messages")
        assert disable_notification  # закріплюємо тихо
        self.pins.append((chat_id, message_id))

    async def unpin_chat_message(self, chat_id, message_id=None):
        self.unpins.append((chat_id, message_id))

    async def edit_message_media(self, media, chat_id, message_id):
        if chat_id in self.edit_fails:
            raise TelegramBadRequest(method=None, message="Bad Request: message can't be edited")
        self.edits.append(("media", chat_id, message_id, media.caption))

    async def edit_message_caption(self, chat_id, message_id, caption, **kw):
        self.edits.append(("caption", chat_id, message_id, caption))

    async def edit_message_text(self, text, chat_id, message_id, **kw):
        self.edits.append(("text", chat_id, message_id, text))


class FakeAlerts:
    def __init__(self):
        self.sent = []

    async def notify(self, key, text, ttl=3600, timeout=15):
        self.sent.append((key, text))
        return True

    def reset(self, key):
        pass


class FakeCallback:
    def __init__(self, from_id, data):
        self.from_user = SimpleNamespace(id=from_id, full_name=f"Власник {from_id}")
        self.data = data
        self.alerts, self.edited, self.markup = [], None, None
        cb = self

        class Msg:
            chat = SimpleNamespace(id=from_id)
            message_id = 0

            async def edit_text(self, text, **kw):
                cb.edited = text

            async def edit_reply_markup(self, reply_markup=None):
                cb.markup = reply_markup

        self.message = Msg()

    async def answer(self, text=None, show_alert=False):
        if show_alert:
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


def make_schedule(day_text="9 жовтня", post_id=10, body=BODY):
    s = parse_post(f"Графік ГПВ на {day_text}.\n" + body, NOW.date())
    s.post_id, s.published = post_id, NOW
    return s


def test_broadcast_records_and_pins(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "kyiv_now", lambda: NOW)
    alerts = FakeAlerts()
    monkeypatch.setattr(publisher, "ALERTS", alerts)
    bot = FakeBot(pin_fails={-300})

    async def scenario(storage, bot):
        await storage.add_chat(-300, "Без прав", "channel", 1)
        await storage.add_chat(-200, "Група", "supergroup", 1)
        await storage.add_chat(-100, "Канал", "channel", 1)
        await storage.add_chat(1, "Власник", "private", 1)
        first = make_schedule()
        assert await publisher.broadcast(bot, storage, first, "u") == 4
        assert sorted(bot.pins) == [(-200, 2), (-100, 3)]  # приватний не закріплюємо; -300 — без прав
        assert [k for k, _ in alerts.sent] == ["pin:-300"]
        assert "Редагувати повідомлення інших" in alerts.sent[0][1]
        assert [c for c, _, _ in await storage.latest_sent_per_chat("2026-10-09")] == [-300, -200, -100, 1]

        # оновлення: нове закріплення, старе знімається; чат без прав не турбуємо добу
        second = make_schedule(post_id=11, body=BODY.replace("2.1 09:00 - 11:00", "2.1 09:30 - 11:00"))
        second.mark_changes(first)
        await publisher.broadcast(bot, storage, second, "u2")
        assert sorted(bot.pins) == [(-200, 2), (-200, 6), (-100, 3), (-100, 7)]
        assert sorted(bot.unpins) == [(-200, 2), (-100, 3)]
        assert len(alerts.sent) == 1
        assert [m for _, m, _ in await storage.latest_sent_per_chat("2026-10-09")] == [5, 6, 7, 8]

        # /pin вимкнули для каналу → там більше не закріплюємо
        await storage.set_pin_enabled(-100, False)
        await publisher.broadcast(bot, storage, second, "u2")
        chats_pinned = [c for c, _ in bot.pins]
        assert chats_pinned.count(-100) == 2 and chats_pinned.count(-200) == 3

    run(tmp_path, scenario, bot)


def test_publish_current_pins_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "kyiv_now", lambda: NOW)

    async def scenario(storage, bot):
        await storage.add_chat(-100, "Канал", "channel", 1)
        for pid, day in ((1, "9"), (2, "10")):
            await storage.save_post(pid, f"Графік ГПВ на {day} жовтня.\n" + BODY, f"u{pid}", POSTED, True)
        assert await publisher.publish_current(bot, storage, -100) == 2 and bot.pins == []  # /now — без закріплення
        assert await publisher.publish_current(bot, storage, -100, pin=True) == 2
        assert bot.pins == [(-100, 4)]  # закріплено останнє надіслане (на завтра)
        assert await storage.latest_sent_per_chat("2026-10-10") == [(-100, 4, "photo")]

    run(tmp_path, scenario)


def test_fix_messages_edits_latest_per_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "kyiv_now", lambda: NOW)
    bot = FakeBot(edit_fails={-200})

    async def scenario(storage, bot):
        await storage.add_chat(-200, "Група без прав", "supergroup", 1)
        await storage.add_chat(-100, "Канал", "channel", 1)
        await storage.add_chat(1, "Я", "private", 1)
        await storage.save_post(10, "Графік ГПВ на 9 жовтня.\n" + BODY, "u10", POSTED, True)
        schedule, url = await publisher.latest_schedule_for_day(storage, date(2026, 10, 9))
        await publisher.broadcast(bot, storage, schedule, url)
        await publisher.broadcast(bot, storage, schedule, url)  # ще раз — /fix має правити лише останні
        # пост виправили (напр. після правки парсера) → /fix замінює вже надіслане
        fixed = BODY.replace("2.1 09:00 - 11:00", "2.1 09:30 - 11:00")
        await storage.save_post(10, "Графік ГПВ на 9 жовтня.\n" + fixed, "u10", POSTED, True)
        ok, failures = await publisher.fix_messages(bot, storage, date(2026, 10, 9))
        assert ok == 2 and len(failures) == 1
        assert failures[0][0] == "Група без прав" and "can't be edited" in failures[0][1]
        assert sorted((c, m) for _, c, m, _ in bot.edits) == [(-100, 5), (1, 6)]
        assert all(kind == "media" and "09:30–11:00" in cap for kind, _, _, cap in bot.edits)
        assert await storage.sent_days() == [("2026-10-09", 3)]
        assert await publisher.fix_messages(bot, storage, date(2026, 10, 12)) == (0, [("—", "немає графіка на цю дату")])

    run(tmp_path, scenario, bot)


def test_fix_and_pin_buttons(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "kyiv_now", lambda: NOW)

    async def scenario(storage, bot):
        await storage.add_chat(-100, "Канал", "channel", 1)
        await storage.save_post(10, "Графік ГПВ на 9 жовтня.\n" + BODY, "u10", POSTED, True)
        schedule, url = await publisher.latest_schedule_for_day(storage, date(2026, 10, 9))
        await publisher.broadcast(bot, storage, schedule, url)

        stranger = FakeCallback(99, "fix:go:2026-10-09")
        await on_fix_button(stranger, storage, bot, OWNERS)
        assert stranger.alerts and bot.edits == []

        cb = FakeCallback(1, "fix:ask:2026-10-09")
        await on_fix_button(cb, storage, bot, OWNERS)
        assert "у 1 повідомленнях за <b>9 жовтня</b>" in cb.edited
        cb = FakeCallback(1, "fix:go:2026-10-09")
        await on_fix_button(cb, storage, bot, OWNERS)
        assert cb.edited.startswith("✅ Оновлено повідомлень за 9 жовтня: 1") and len(bot.edits) == 1

        pin = FakeCallback(1, "pin:-100")
        await on_pin_button(pin, storage, OWNERS)
        assert (await storage.pin_state(-100))[1] is False
        assert pin.markup.inline_keyboard[0][0].text.startswith("❌ 📢 Канал")

    run(tmp_path, scenario)


def test_caption_has_queue_hint():
    cap = format_caption(make_schedule(), "https://t.me/x/1")
    assert "cherkasyoblenergo.com/off" in cap and cap.index("Своя черга") < cap.index("Джерело")


def test_is_announcement():
    for text in (GOP, NEWS, METERS, GENERIC_EMERGENCY):
        assert not is_announcement(text), text
    for text in (CANCEL, GAV, NO_OUTAGES, LOCAL_EMERGENCY):
        assert is_announcement(text), text
    body = format_announcement("x" * 5000, "https://t.me/x/1")
    assert body.startswith("📢 <b>Черкасиобленерго</b>") and len(body) < 4096 and body.endswith("Джерело</a>")


def test_announcement_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "kyiv_now", lambda: NOW)
    feed = []

    async def fake_fetch(client, channel):
        return list(feed)

    monkeypatch.setattr(publisher, "fetch_posts", fake_fetch)

    async def scenario(storage, bot):
        await storage.add_chat(-100, "Канал", "channel", 1)
        w = publisher.Watcher(bot, storage, "x", 60, owner_ids=OWNERS)
        feed.append(Post(1, "Графік ГПВ на 9 жовтня.\n" + BODY, datetime(2026, 10, 9, 5, 0, tzinfo=UTC), "u1"))
        await w.check_once(None)  # перший запуск — усе позначено прочитаним
        feed.append(Post(2, CANCEL, datetime(2026, 10, 9, 6, 0, tzinfo=UTC), "https://t.me/x/2"))
        feed.append(Post(3, NEWS, datetime(2026, 10, 9, 6, 1, tzinfo=UTC), "https://t.me/x/3"))
        await w.check_once(None)
        prompts = [(c, t) for c, t, _ in bot.texts if "Оголошення обленерго" in t]
        assert sorted(c for c, _ in prompts) == [1, 2] and "скасовано" in prompts[0][1]
        assert await storage.announcement_status(2) == "pending" and await storage.announcement_status(3) is None
        await w.check_once(None)  # повторна перевірка не дублює запит
        assert len([1 for _, t, _ in bot.texts if "Оголошення обленерго" in t]) == 2

        stranger = FakeCallback(99, "ann:pub:2")
        await on_announcement_button(stranger, storage, bot, OWNERS)
        assert stranger.alerts and await storage.announcement_status(2) == "pending"

        cb = FakeCallback(2, "ann:pub:2")
        await on_announcement_button(cb, storage, bot, OWNERS)
        published = [(c, t) for c, t, _ in bot.texts if t.startswith("📢")]
        assert [c for c, _ in published] == [-100]
        assert "скасовано" in published[0][1] and "https://t.me/x/2" in published[0][1]
        assert await storage.announcement_status(2) == "published"
        assert len(bot.edits) == 2 and all("Опубліковано в 1 чатів" in e[3] and "Власник 2" in e[3] for e in bot.edits)
        assert await storage.sent_for_post(2) == [(-100, 3, "text")]

        again = FakeCallback(1, "ann:skip:2")
        await on_announcement_button(again, storage, bot, OWNERS)
        assert again.alerts == ["Уже вирішено"]

    run(tmp_path, scenario)
