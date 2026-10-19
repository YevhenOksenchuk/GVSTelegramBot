"""Надійність: алерти власникам, бекап бази, сторожовий таймер, конфіг."""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aiogram.exceptions import TelegramForbiddenError

from bot import publisher
from bot.alerts import Alerts
from bot.main import WATCHDOG_STALE, watchdog_reason
from bot.maintenance import LAST_BACKUP_AT, LAST_BACKUP_DAY, backup_db, backup_due, send_backup
from bot.parser import unrecognized_lines
from bot.scraper import Post, parse_channel_html
from bot.storage import Storage
from tests.test_dates import BODY, NOW
from tests.test_parser import SAMPLE

OWNERS = frozenset({1, 2})


class FakeBot:
    def __init__(self, fail_for=()):
        self.messages, self.documents = [], []
        self.fail_for = set(fail_for)

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.fail_for:
            raise RuntimeError("Forbidden: bot can't initiate conversation with a user")
        self.messages.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.messages))

    async def send_document(self, chat_id, document, caption=None, **kw):
        self.documents.append((chat_id, document, caption))

    async def send_photo(self, chat_id, photo, caption, parse_mode):
        raise TelegramForbiddenError(method=None, message="Forbidden: bot was kicked from the channel chat")


class FakeAlerts:
    def __init__(self):
        self.sent = []

    async def notify(self, key, text, ttl=3600, timeout=15):
        self.sent.append((key, text))
        return True

    def reset(self, key):
        pass


def test_alerts_throttle_and_reset():
    bot = FakeBot(fail_for={2})
    alerts = Alerts(bot, OWNERS)

    async def main():
        assert await alerts.notify("k", "перший")
        assert not await alerts.notify("k", "повтор у межах ttl — тиша")
        assert await alerts.notify("other", "інший ключ")
        alerts.reset("k")
        assert await alerts.notify("k", "після reset")
        assert await alerts.notify("k", "ttl=0 — завжди", ttl=0)
        assert not await Alerts(None, OWNERS).notify("x", "без бота — лише лог")

    asyncio.run(main())
    assert [c for c, _ in bot.messages] == [1, 1, 1, 1]  # власнику 2 не доставилось, решта працює


def test_unrecognized_lines():
    assert unrecognized_lines(SAMPLE) == []
    bad = SAMPLE.replace("5.2 00:00 - 01:00", "5.3 00:00 - 01:00")
    assert unrecognized_lines(bad) == ["5.3 00:00 - 01:00, 10:00 - 12:00, 19:00 - 21:00"]


def test_scraper_has_photo():
    html = """
    <div class="tgme_widget_message" data-post="c/5"><a class="tgme_widget_message_photo_wrap" href="x"></a>
      <div class="tgme_widget_message_text">Графік на завтра — на фото</div><time datetime="2026-10-10T10:00:00+00:00"></time></div>
    <div class="tgme_widget_message" data-post="c/6">
      <div class="tgme_widget_message_text">текст</div><time datetime="2026-10-10T10:01:00+00:00"></time></div>"""
    assert [(p.post_id, p.has_photo) for p in parse_channel_html(html)] == [(5, True), (6, False)]


def _post(pid, text, hour, **kw):
    return Post(pid, text, datetime(2026, 10, 9, hour, 0, tzinfo=timezone.utc), f"https://t.me/x/{pid}", **kw)


def test_watcher_alerts_on_bad_lines_and_photo(tmp_path, monkeypatch):
    fake = FakeAlerts()
    monkeypatch.setattr(publisher, "ALERTS", fake)
    monkeypatch.setattr(publisher, "kyiv_now", lambda: NOW)
    feed = []

    async def fake_fetch(client, channel):
        return list(feed)

    monkeypatch.setattr(publisher, "fetch_posts", fake_fetch)

    async def main():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            w = publisher.Watcher(FakeBot(), storage, "x", 60)
            feed.append(_post(1, "Графік ГПВ на 9 жовтня.\n" + BODY, 5))
            await w.check_once(None)  # перший запуск — без алертів
            assert fake.sent == []
            feed.append(_post(2, "Оновлено графік ГПВ на 9 жовтня.\n" + BODY.replace("5.2 00:00", "5.3 00:00"), 6))
            feed.append(_post(3, "Графік відключень (ГПВ) на 10 жовтня — дивіться на картинці", 7, has_photo=True))
            feed.append(_post(4, "Новини енергетики з фото", 7, has_photo=True))
            await w.check_once(None)
            assert [k for k, _ in fake.sent] == ["parse:2", "photo:3"]
            assert "5.3 00:00" in fake.sent[0][1] and "https://t.me/x/3" in fake.sent[1][1]
        finally:
            await storage.close()

    asyncio.run(main())


def test_lost_access_alert_only_for_channels(tmp_path, monkeypatch):
    fake = FakeAlerts()
    monkeypatch.setattr(publisher, "ALERTS", fake)

    async def main():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            await storage.add_chat(-100, "Зоя Вовк", "channel", 1)
            await storage.add_chat(5, "Yevhen", "private", 5)
            bot = FakeBot()
            assert not await publisher.send_schedule(bot, storage, -100, b"png", "cap")
            assert not await publisher.send_schedule(bot, storage, 5, b"png", "cap")
            assert await storage.active_chats() == []
            assert [k for k, _ in fake.sent] == ["lost:-100"]  # блокування в особистих — не проблема
            assert "Зоя Вовк" in fake.sent[0][1]
        finally:
            await storage.close()

    asyncio.run(main())


def test_source_down_and_up_alerts(monkeypatch):
    fake = FakeAlerts()
    monkeypatch.setattr(publisher, "ALERTS", fake)
    clock = {"now": NOW}
    monkeypatch.setattr(publisher, "kyiv_now", lambda: clock["now"])
    w = publisher.Watcher(None, None, "x", 60)

    async def main():
        await w.on_failure("ConnectError: boom")  # перша помилка — лише запам'ятали
        clock["now"] += timedelta(minutes=5)
        await w.on_failure("ConnectError: boom")
        assert fake.sent == []
        clock["now"] += timedelta(minutes=6)
        await w.on_failure("ConnectError: boom")  # 11 хв без каналу — алерт
        assert [k for k, _ in fake.sent] == ["source_down"] and "11 хв" in fake.sent[0][1]
        clock["now"] += timedelta(minutes=1)
        await w.on_success()
        assert [k for k, _ in fake.sent] == ["source_down", "source_up"]
        assert w.failing_since is None and w.last_error is None and w.last_success == clock["now"]
        await w.on_failure("x")  # коротка перерва — без алертів
        clock["now"] += timedelta(minutes=2)
        await w.on_success()
        assert len(fake.sent) == 2

    asyncio.run(main())


def test_backup_db_and_send(tmp_path):
    async def main():
        storage = Storage(str(tmp_path / "bot.db"))
        await storage.open()
        try:
            await storage.add_chat(-1, "Канал", "channel", 1)
            await storage.set_user(7, "allowed", "Зоя", "zoya")
            dest = str(tmp_path / "copy.db")
            assert await backup_db(storage, dest) > 0
            copy = sqlite3.connect(dest)
            assert copy.execute("select count(*) from chats").fetchone()[0] == 1
            copy.close()

            bot = FakeBot()
            assert await send_backup(bot, storage, OWNERS) == 2
            assert sorted(c for c, _, _ in bot.documents) == [1, 2]
            assert "активних чатів: 1" in bot.documents[0][2] and "користувачів: 1" in bot.documents[0][2]
            assert await storage.get_meta(LAST_BACKUP_DAY) == publisher.kyiv_now().strftime("%Y-%m-%d")
            assert await storage.get_meta(LAST_BACKUP_AT)
        finally:
            await storage.close()

    asyncio.run(main())


def test_backup_due():
    assert not backup_due(NOW.replace(hour=3, minute=59), 4, None)
    assert not backup_due(NOW.replace(hour=4, minute=4), 4, None)
    assert backup_due(NOW.replace(hour=4, minute=5), 4, None)
    assert backup_due(NOW.replace(hour=15), 4, "2026-10-08")  # після рестарту — надолужуємо
    assert not backup_due(NOW.replace(hour=15), 4, "2026-10-09")


def test_watchdog_reason():
    w = publisher.Watcher(None, None, "x", 60)
    assert watchdog_reason(w, True, NOW, NOW) == "цикл перевірки каналу завершився"
    assert watchdog_reason(w, False, NOW, NOW + timedelta(minutes=5)) is None  # щойно запустились
    late = NOW + timedelta(seconds=WATCHDOG_STALE + 1)
    assert watchdog_reason(w, False, NOW, late)  # жодної спроби за 10 хв
    w.last_check = late - timedelta(minutes=2)
    assert watchdog_reason(w, False, NOW, late) is None
    w.last_check = late - timedelta(minutes=11)
    assert "понад 10 хв" in watchdog_reason(w, False, NOW, late)


def test_config_defaults(monkeypatch):
    from bot.config import load_config

    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.delenv("BOT_VERSION", raising=False)
    monkeypatch.setenv("BACKUP_HOUR", "30")
    monkeypatch.setenv("HEALTHCHECK_URL", " https://hc-ping.com/abc ")
    cfg = load_config()
    assert cfg.bot_version == "dev" and cfg.backup_hour == 23 and cfg.healthcheck_url == "https://hc-ping.com/abc"
