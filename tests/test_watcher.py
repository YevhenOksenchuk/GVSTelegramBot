import asyncio
import copy
from types import SimpleNamespace
from pathlib import Path

from bot import publisher
from bot.scraper import parse_channel_html
from bot.storage import Storage

HTML = (Path(__file__).parent / "channel_sample.html").read_text(encoding="utf-8")


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_photo(self, chat_id, photo, caption, parse_mode):
        self.sent.append((chat_id, caption))
        return SimpleNamespace(message_id=len(self.sent))

    async def pin_chat_message(self, chat_id, message_id, disable_notification=False):
        pass

    async def unpin_chat_message(self, chat_id, message_id=None):
        pass


def test_watcher_first_run_then_new_and_edited(tmp_path, monkeypatch):
    posts = parse_channel_html(HTML)
    sched = [p for p in posts if "1.1" in p.text and "електропостачання" in p.text][-1]
    feed = [p for p in posts if p.post_id < sched.post_id]

    async def fake_fetch(client, channel):
        return list(feed)

    monkeypatch.setattr(publisher, "fetch_posts", fake_fetch)
    monkeypatch.setattr(publisher, "kyiv_now", lambda: sched.posted_at.astimezone(publisher.KYIV))

    async def scenario():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        await storage.add_chat(-100, "Канал", "channel", 1)
        await storage.add_chat(-200, "Група", "supergroup", 1)
        try:
            await _run(storage)
        finally:
            await storage.close()

    async def _run(storage):
        bot = FakeBot()
        w = publisher.Watcher(bot, storage, "x", 60)

        await w.check_once(None)            # перший запуск — нічого не шлемо
        assert bot.sent == []
        feed.append(sched)                  # новий пост з графіком
        await w.check_once(None)
        assert sorted(c for c, _ in bot.sent) == [-200, -100]
        await w.check_once(None)            # повторно не шлемо
        assert len(bot.sent) == 2
        sched.text = sched.text.replace("05:00 - 07:00", "06:00 - 07:00", 1)  # редагування
        await w.check_once(None)
        assert len(bot.sent) == 4 and "Оновлений" in bot.sent[-1][1]
        assert "1.1</b>" in bot.sent[-1][1] and "✏️" in bot.sent[-1][1]

        sched.text = sched.text.replace("Через постійні", "Внаслідок")  # правка без зміни годин
        await w.check_once(None)
        assert len(bot.sent) == 4

        repost = copy.copy(sched)                    # окремий пост «Оновлений графік», ті самі години
        repost.post_id = sched.post_id + 1
        repost.text = "Оновлений графік ГПВ на 9 жовтня.\n" + sched.text
        feed.append(repost)
        await w.check_once(None)
        assert len(bot.sent) == 4

        changed = copy.copy(repost)                  # новий пост зі зміненими годинами
        changed.post_id = repost.post_id + 1
        changed.text = repost.text.replace("2.1 09:00 - 11:00", "2.1 09:30 - 11:00")
        feed.append(changed)
        await w.check_once(None)
        assert len(bot.sent) == 6
        cap = bot.sent[-1][1]
        assert "🔄" in cap and "оновлено о" in cap and "09:30–11:00 · 17:00–19:00  ✏️" in cap

    asyncio.run(scenario())
