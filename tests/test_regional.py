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


def test_fix_converts_wrongly_published_regional_post(tmp_path, monkeypatch):
    """Як 17 листопада: уманський графік пішов як обласний і закріпився; /fix робить з нього індивідуальний."""
    monkeypatch.setattr(publisher, "kyiv_now", lambda: datetime(2026, 11, 17, 12, 0, tzinfo=publisher.KYIV))

    async def scenario():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            await storage.add_chat(-100, "Канал", "channel", 1)
            await storage.save_post(2027, "Графік ГПВ на 17 листопада.\n" + BODY, "u2027",
                                    datetime(2026, 11, 16, 23, 45, tzinfo=publisher.KYIV).isoformat(), True)
            await storage.save_post(2028, TEXT, "u2028",
                                    datetime(2026, 11, 17, 9, 8, tzinfo=publisher.KYIV).isoformat(), True)
            await storage.record_sent(-100, 10, 2027, "2026-11-17", "photo")  # правильний обласний
            await storage.record_sent(-100, 11, 2028, "2026-11-17", "photo")  # помилково як «оновлення»
            await storage.set_pinned(-100, 11)
            bot = FakeBot()
            ok, failures = await publisher.fix_messages(bot, storage, date(2026, 11, 17))
            assert failures == [] and ok == 2
            by_msg = {m: cap for _, _, m, cap in bot.edits}
            assert "Індивідуальний графік — Частина Уманського району" in by_msg[11]
            assert "Графік відключень (ГПВ)" in by_msg[10] and "Індивідуальний" not in by_msg[10]
            assert bot.pins == [(-100, 10)] and bot.unpins == [(-100, 11)]
            assert await storage.latest_sent_per_chat("2026-11-17") == [(-100, 10, "photo")]
        finally:
            await storage.close()

    asyncio.run(scenario())


COMBINED = (Path(__file__).parent / "real_combined_18nov.txt").read_text(encoding="utf-8")


def test_combined_post_is_split_into_oblast_and_regional():
    """18 листопада: в одному пості і графік по області, і індивідуальний для Уманщини."""
    s = parse_post(COMBINED, date(2026, 11, 17))
    assert s and s.day == date(2026, 11, 18) and len(s.queues) == 12
    assert s.queues["1.2"] == [(0, 120), (360, 480), (720, 840), (1080, 1260)]  # не змішано з уманським
    assert s.queues["5.1"] == [(240, 360), (600, 720), (900, 1020)]
    r = parse_regional(COMBINED, date(2026, 11, 17))
    assert r.region == "частина Уманського району" and r.day == date(2026, 11, 18)
    assert sorted(r.queues) == ["1.2", "2.2", "3.2", "4.2", "5.2", "6.2"]
    assert r.queues["1.2"] == [(0, 180), (360, 540), (720, 900), (1080, 1260)]
    assert "Христинівської філій" in r.region_note
    assert parse_post(TEXT, date(2026, 11, 17)) is None  # суто районний пост — як і раніше


def _combined_env(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "kyiv_now", lambda: datetime(2026, 11, 17, 22, 0, tzinfo=publisher.KYIV))
    feed = []

    async def fake_fetch(client, channel, before=None):
        return list(feed)

    monkeypatch.setattr(publisher, "fetch_posts", fake_fetch)
    return feed


def test_watcher_publishes_both_parts_of_combined_post(tmp_path, monkeypatch):
    feed = _combined_env(tmp_path, monkeypatch)

    async def scenario():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            await storage.add_chat(-100, "Канал", "channel", 1)
            bot = FakeBot()
            w = publisher.Watcher(bot, storage, "x", 60, owner_ids=frozenset({1}))
            feed.append(Post(1, "x", datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc), "u1"))
            await w.check_once(None)
            feed.append(Post(2032, COMBINED, datetime(2026, 11, 17, 19, 43, tzinfo=timezone.utc), "u"))
            await w.check_once(None)
            await w.check_once(None)
            assert len(bot.photos) == 2
            assert "Графік відключень (ГПВ)</b> — <b>18 листопада" in bot.photos[0][1]
            assert "Індивідуальний графік — Частина Уманського району" in bot.photos[1][1]
            assert bot.pins == [(-100, 1)]  # закріплено обласний
            assert await storage.latest_sent_per_chat("2026-11-18") == [(-100, 1, "photo")]
            # /fix не перетворює обласне повідомлення на індивідуальне
            ok, failures = await publisher.fix_messages(bot, storage, date(2026, 11, 18))
            assert failures == [] and ok == 1 and "Індивідуальний" not in bot.edits[-1][3]
        finally:
            await storage.close()

    asyncio.run(scenario())


def test_reparse_after_update_publishes_missed_oblast_and_repairs_regional(tmp_path, monkeypatch):
    """Старий код опублікував мішаний пост як індивідуальний; у каналі це повідомлення вже видалили вручну."""
    _combined_env(tmp_path, monkeypatch)

    async def scenario():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            await storage.add_chat(-100, "Канал", "channel", 1)
            await storage.add_chat(-200, "Група", "supergroup", 1)
            await storage.save_post(2032, COMBINED, "u", datetime(2026, 11, 17, 19, 43, tzinfo=timezone.utc).isoformat(),
                                    False)
            await storage.record_sent(-100, 50, 2032, None, "photo")
            await storage.record_sent(-200, 51, 2032, None, "photo")
            bot = FakeBot(edit_fails={-100})
            w = publisher.Watcher(bot, storage, "x", 60, owner_ids=frozenset({1}))
            assert await w.reparse_recent() == 1
            oblast = [c for c in bot.photos if "Графік відключень (ГПВ)" in c[1]]
            assert sorted(c[0] for c in oblast) == [-200, -100] and len(bot.pins) == 2
            assert [(e[1], e[2]) for e in bot.edits] == [(-200, 51)] and "Індивідуальний" in bot.edits[0][3]
            resent = [c for c in bot.photos if "Індивідуальний" in c[1]]
            assert [c[0] for c in resent] == [-100]  # там, де старе видалили, — надіслано заново
            assert [s.post_id for s, _ in await publisher.current_schedules(storage)] == [2032]
            assert await w.reparse_recent() == 0 and len(bot.photos) == 3  # повторний запуск нічого не шле
        finally:
            await storage.close()

    asyncio.run(scenario())
