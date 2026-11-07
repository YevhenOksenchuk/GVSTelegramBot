"""Обленерго видалило пост: бот питає власників і за їх згодою прибирає свої повідомлення."""
import asyncio
from datetime import datetime, timezone

from bot import publisher
from bot.handlers import on_deleted_button
from bot.scraper import Post
from bot.storage import Storage
from tests.test_channel import FakeBot, FakeCallback
from tests.test_dates import BODY, NOW
from tests.test_gav import START

OWNERS = frozenset({1, 2})
UTC = timezone.utc


def test_deleted_post_prompt_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "kyiv_now", lambda: NOW)
    feed = []

    async def fake_fetch(client, channel, before=None):
        return list(feed)

    monkeypatch.setattr(publisher, "fetch_posts", fake_fetch)

    async def scenario():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            await storage.add_chat(-100, "Канал", "channel", 1)
            await storage.set_meta("history_backfilled", "x")
            bot = FakeBot()
            w = publisher.Watcher(bot, storage, "x", 60, owner_ids=OWNERS)
            v1 = Post(1, "Графік ГПВ на 9 жовтня.\n" + BODY, datetime(2026, 10, 8, 20, 0, tzinfo=UTC), "u1")
            feed.append(v1)
            await w.check_once(None)  # перший запуск
            v2 = Post(2, "Оновлено графік ГПВ на 9 жовтня.\n" + BODY.replace("2.1 09:00 - 11:00", "2.1 09:30 - 11:00"),
                      datetime(2026, 10, 9, 6, 0, tzinfo=UTC), "u2")
            gav = Post(3, START, datetime(2026, 10, 9, 7, 0, tzinfo=UTC), "https://t.me/x/3")
            feed.extend([v2, gav])
            await w.check_once(None)
            assert [c for c, _ in bot.photos] == [-100] and "09:30" in bot.photos[0][1]  # оновлення опубліковано
            assert any("ГАВ" in t for _, t, _ in bot.texts)
            schedule_msg = await storage.sent_for_post(2)
            gav_msg = await storage.sent_for_post(3)
            assert len(schedule_msg) == 1 and len(gav_msg) == 1

            # обленерго видалило пост про ГАВ (3) — на сторінці лишились 1, 2 і новий 4
            feed.remove(gav)
            feed.append(Post(4, "Новини енергетики", datetime(2026, 10, 9, 7, 30, tzinfo=UTC), "u4"))
            await w.check_once(None)
            prompts = [(c, t, kw) for c, t, kw in bot.texts if "видалило" in t]
            assert sorted(c for c, _, _ in prompts) == [1, 2] and "https://t.me/x/3" in prompts[0][1]
            assert "1 повідомл" in prompts[0][1]
            await w.check_once(None)  # вдруге не питаємо
            assert len([1 for _, t, _ in bot.texts if "видалило" in t]) == 2

            stranger = FakeCallback(99, "del:yes:3")
            await on_deleted_button(stranger, storage, bot, OWNERS)
            assert stranger.alerts and bot.deleted == []

            cb = FakeCallback(1, "del:yes:3")
            await on_deleted_button(cb, storage, bot, OWNERS)
            assert bot.deleted == [(gav_msg[0][0], gav_msg[0][1])]
            assert await storage.sent_for_post(3) == []
            assert len(bot.edits) == 2 and all("Прибрано повідомлень: 1" in e[3] for e in bot.edits)
            again = FakeCallback(2, "del:no:3")
            await on_deleted_button(again, storage, bot, OWNERS)
            assert again.alerts == ["Уже вирішено"]

            # видалили оновлення графіка (2) — чинною знову стає перша версія, нагадування/статистика її беруть
            feed.remove(v2)
            await w.check_once(None)
            current = await publisher.current_schedules(storage)
            assert current[0][0].post_id == 1 and current[0][0].queues["2.1"][0] == (540, 660)
            assert len([1 for _, t, _ in bot.texts if "видалило" in t]) == 4
        finally:
            await storage.close()

    asyncio.run(scenario())
