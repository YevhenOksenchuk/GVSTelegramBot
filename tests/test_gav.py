"""ГАВ (аварійні відключення): автоматична коротка публікація без модерації."""
import asyncio
from datetime import datetime, timezone

from bot import publisher
from bot.announcements import classify_gav, format_gav, gav_note, is_announcement, is_special
from bot.scraper import Post
from bot.storage import Storage
from tests.test_channel import FakeBot
from tests.test_dates import BODY, NOW

OWNERS = frozenset({1, 2})
START = ("Через наслідки попередніх масованих ракетно-дронових атак 4 листопада з 05:12 по Черкаській області "
         "застосовані графіки аварійних відключень (ГАВ) за командою НЕК \"Укренерго\".\n\n"
         "Також продовжують діяти графіки погодинних відключень (ГПВ).")
START_OLD = "3 липня з 13:44 по Черкаській області застосовані графіки аварійних відключень (ГАВ) за командою НЕК."
STOP = "З 15:20 по Черкаській області скасовано графіки аварійних відключень (ГАВ)."
STOP2 = "ГАВ припинено о 18:05, діють лише ГПВ."
AD = "У чат-ботах можна дізнатися свою чергу, розділ «Відключення» — «Черги ГПВ/ГАВ»."
GENERIC = "У низці регіонів України, зокрема у Києві, вимушено застосовуються аварійні відключення електроенергії."


SPECIAL = ('7 листопада з 06:44 по Черкаській області скасовано графіки аварійних відключень (ГАВ) та '
           'застосовані спеціальні графіки аварійних відключень (СГАВ) за командою НЕК "Укренерго".')
SPECIAL_ONLY = "7 листопада з 06:44 по Черкаській області застосовані спеціальні графіки аварійних відключень (СГАВ)."
SPECIAL_STOP = "З 18:05 скасовано спеціальні графіки аварійних відключень (СГАВ)."


def test_special_schedules_take_precedence_over_cancellation():
    assert classify_gav(SPECIAL) == ("special", "06:44")  # «застосовано» важливіше за «скасовано» в тому ж пості
    assert classify_gav(SPECIAL_ONLY) == ("special", "06:44")
    assert classify_gav(SPECIAL_STOP) == ("stop", "18:05") and is_special(SPECIAL_STOP)
    assert format_gav("special", "06:44", "u") == (
        "🚨 Спеціальні графіки аварійних відключень (СГАВ) з 06:44\n\n<a href=\"u\">Черкасиобленерго</a>")
    assert format_gav("stop", "18:05", "u", special_stop=True).startswith(
        "✅ Спеціальні графіки аварійних відключень (СГАВ) скасовано з 18:05")


def test_classify_and_format():
    assert classify_gav(START) == ("start", "05:12")
    assert classify_gav(START_OLD) == ("start", "13:44")
    assert classify_gav(STOP) == ("stop", "15:20")
    assert classify_gav(STOP2) == ("stop", "18:05")
    assert classify_gav(AD) is None and classify_gav(GENERIC) is None  # незрозуміле — не публікуємо наосліп
    assert gav_note(START) == "Також продовжують діяти графіки погодинних відключень (ГПВ)." and gav_note(STOP) is None
    assert format_gav("start", "05:12", "u", gav_note(START)) == (
        "🚨 Аварійні відключення (ГАВ) з 05:12\n\n"
        "Також продовжують діяти графіки погодинних відключень (ГПВ).\n\n"
        '<a href="u">Черкасиобленерго</a>')
    assert format_gav("stop", None, "u") == '✅ Аварійні відключення (ГАВ) скасовано\n\n<a href="u">Черкасиобленерго</a>'
    assert is_announcement(AD) is False or classify_gav(AD) is None  # реклама чат-ботів не має публікуватись сама


def test_watcher_publishes_gav_without_moderation(tmp_path, monkeypatch):
    clock = {"now": NOW}
    monkeypatch.setattr(publisher, "kyiv_now", lambda: clock["now"])
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
            feed.append(Post(1, "Графік ГПВ на 9 жовтня.\n" + BODY, datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc), "u1"))
            await w.check_once(None)  # перший запуск

            feed.append(Post(2, START, datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc), "https://t.me/x/2"))
            await w.check_once(None)
            published = [(c, t, kw) for c, t, kw in bot.texts if "ГАВ" in t]
            assert [c for c, _, _ in published] == [-100]  # одразу в канал, без запиту власникам
            assert published[0][1].startswith("🚨 Аварійні відключення (ГАВ) з 05:12\n\nТакож продовжують діяти")
            assert not [t for _, t, _ in bot.texts if "Оголошення обленерго" in t]
            assert await storage.announcement_status(2) == "published"

            await w.check_once(None)  # повторна перевірка — без дублю
            assert len([1 for _, t, _ in bot.texts if "ГАВ" in t]) == 1

            clock["now"] = NOW.replace(hour=2, minute=30)  # уночі — без звуку
            feed.append(Post(3, STOP, datetime(2026, 10, 9, 23, 30, tzinfo=timezone.utc), "https://t.me/x/3"))
            await w.check_once(None)
            assert bot.texts[-1][1].startswith("✅ Аварійні відключення (ГАВ) скасовано з 15:20")
            assert bot.texts[-1][2] and bot.texts[-1][2].get("disable_notification") is True
            assert published[0][2].get("disable_notification") is False
        finally:
            await storage.close()

    asyncio.run(scenario())
