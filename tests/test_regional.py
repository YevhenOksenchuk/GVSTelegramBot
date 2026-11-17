"""Індивідуальні графіки окремого району: публікуються окремо й не зачіпають обласний графік."""
import asyncio
from datetime import date, datetime, timezone
from pathlib import Path

from bot import publisher
from bot.formatter import CAPTION_LIMIT, format_regional_caption, tg_len
from bot.parser import parse_post, parse_regional
from bot.render import render_schedule
from bot.scraper import Post
from bot.storage import Storage
from tests.test_channel import FakeBot
from tests.test_dates import BODY, NOW

TEXT = (Path(__file__).parent / "real_regional_17nov.txt").read_text(encoding="utf-8")


def test_parse_and_format_real_post():
    s = parse_regional(TEXT, date(2026, 11, 17))
    assert s.region == "частина Уманського району" and s.day == date(2026, 11, 17)
    assert sorted(s.queues) == ["1.2", "2.2", "3.2", "4.2", "5.2", "6.2"]
    assert s.queues["4.2"] == [(540, 720), (900, 1080), (1260, 1440)]
    assert "Жашківської філії" in s.region_note
    cap = format_regional_caption(s, "u")
    assert cap.startswith("📍 <b>Індивідуальний графік — Частина Уманського району</b>, <b>17 листопада</b>")
    assert "<b>1.1</b>" not in cap and "<b>1.2</b>  06:00–09:00 · 12:00–15:00 · 18:00–21:00" in cap
    assert "Для решти області діє звичайний графік ГПВ." in cap and tg_len(cap) <= CAPTION_LIMIT
    assert render_schedule(s)[:8] == b"\x89PNG\r\n\x1a\n"
    assert parse_regional("Графік ГПВ на 9 жовтня.\n" + BODY) is None  # обласний — не індивідуальний


def test_watcher_publishes_regional_separately(tmp_path, monkeypatch):
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
            w = publisher.Watcher(bot, storage, "x", 60, owner_ids=frozenset({1}))
            feed.append(Post(1, "Графік ГПВ на 9 жовтня.\n" + BODY, datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc), "u1"))
            await w.check_once(None)
            regional = TEXT.replace("Унаслідок", "9 жовтня унаслідок")
            feed.append(Post(2, regional, datetime(2026, 10, 9, 7, 0, tzinfo=timezone.utc), "u2"))
            await w.check_once(None)
            assert len(bot.photos) == 1 and bot.photos[0][0] == -100
            assert "Індивідуальний графік — Частина Уманського району" in bot.photos[0][1]
            assert bot.pins == []  # не закріплюємо замість обласного
            await w.check_once(None)
            assert len(bot.photos) == 1  # без дублів
            # обласний графік, /now, нагадування й статистика лишаються на пості 1
            current = await publisher.current_schedules(storage)
            assert [s.post_id for s, _ in current] == [1] and current[0][0].region is None
            assert await storage.sent_days() == []  # /fix індивідуальний пост не чіпає
            assert len(await storage.sent_for_post(2)) == 1  # але видалення обленерго ми помітимо
        finally:
            await storage.close()

    asyncio.run(scenario())
