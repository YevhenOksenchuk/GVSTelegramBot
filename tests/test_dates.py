"""Графіки на різні дати: сьогодні / завтра та їх оновлення вперемішку."""
import asyncio
from datetime import datetime, timezone

from bot import publisher
from bot.scraper import Post
from bot.storage import Storage
from tests.test_parser import SAMPLE

BODY = SAMPLE.split("електропостачання:")[1]
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=publisher.KYIV)  # «сьогодні» = 9 жовтня


def post(pid, text, hour):
    return Post(pid, text, datetime(2026, 10, 9, hour, 0, tzinfo=timezone.utc), f"https://t.me/x/{pid}")


def change(body, old, new):
    assert old in body
    return body.replace(old, new)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_photo(self, chat_id, photo, caption, parse_mode):
        self.sent.append(caption)


def run(tmp_path, monkeypatch, scenario):
    feed = []

    async def fake_fetch(client, channel):
        return list(feed)

    monkeypatch.setattr(publisher, "fetch_posts", fake_fetch)
    monkeypatch.setattr(publisher, "kyiv_now", lambda: NOW)

    async def main():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            await storage.add_chat(-100, "Канал", "channel", 1)
            bot = FakeBot()
            w = publisher.Watcher(bot, storage, "x", 60)
            feed.append(post(1, "Графік ГПВ на 9 жовтня.\n" + BODY, 5))  # вже був до запуску
            await w.check_once(None)
            assert bot.sent == []
            await scenario(feed, w, bot, storage)
        finally:
            await storage.close()

    asyncio.run(main())


def test_tomorrow_then_today_update_then_tomorrow_update(tmp_path, monkeypatch):
    async def scenario(feed, w, bot, storage):
        # 1) графік на завтра
        feed.append(post(2, "Графік ГПВ на 10 жовтня.\n" + BODY, 8))
        await w.check_once(None)
        assert len(bot.sent) == 1 and "10 жовтня" in bot.sent[-1] and "Оновлений" not in bot.sent[-1]

        # 2) оновлення на сьогодні — порівнюється з графіком на 9-те, а не з завтрашнім
        today_upd = change(BODY, "21:00 - 23:00\n\n1.2", "20:00 - 23:00\n\n1.2")  # 1.1: 21→20
        feed.append(post(3, "Оновлений графік ГПВ на 9 жовтня.\n" + today_upd, 10))
        await w.check_once(None)
        cap = bot.sent[-1]
        assert len(bot.sent) == 2 and "9 жовтня" in cap and "Оновлений" in cap
        assert "1.1</b>  05:00–07:00 · 15:00–17:00 · 20:00–23:00  ✏️" in cap and "було: 15:00–17:00 · 21:00–23:00" in cap
        assert cap.count("✏️") == 2  # одна підчерга + пояснення внизу

        # 3) оновлення на завтра — порівнюється із завтрашнім (п.1), а не з сьогоднішнім (п.2)
        tomorrow_upd = change(BODY, "6.2 00:00 - 01:00", "6.2 02:00 - 03:00")
        feed.append(post(4, "Оновлений графік ГПВ на 10 жовтня.\n" + tomorrow_upd, 12))
        await w.check_once(None)
        cap = bot.sent[-1]
        assert len(bot.sent) == 3 and "10 жовтня" in cap
        assert "6.2</b>  02:00–03:00" in cap and "1.1</b>  05:00–07:00" in cap
        assert cap.count("✏️") == 2

        # /now: найсвіжіший на сьогодні, потім найсвіжіший на завтра
        current = await publisher.current_schedules(storage)
        assert [(s.date_label, url) for s, url in current] == [
            ("9 жовтня", "https://t.me/x/3"),
            ("10 жовтня", "https://t.me/x/4"),
        ]

    run(tmp_path, monkeypatch, scenario)


def test_two_dates_in_one_check_both_sent(tmp_path, monkeypatch):
    async def scenario(feed, w, bot, storage):
        today_upd = change(BODY, "3.1 03:00 - 05:00, 13:00 - 15:00", "3.1 03:00 - 05:00, 13:00 - 14:30")
        feed.append(post(2, "Оновлений графік ГПВ на 9 жовтня.\n" + today_upd, 6))
        feed.append(post(3, "Графік ГПВ на 10 жовтня.\n" + BODY, 6))
        await w.check_once(None)  # обидва пости побачили за одну перевірку
        assert len(bot.sent) == 2
        assert "9 жовтня" in bot.sent[0] and "10 жовтня" in bot.sent[1]

    run(tmp_path, monkeypatch, scenario)


def test_old_date_not_sent_but_same_day_twice_sends_latest(tmp_path, monkeypatch):
    async def scenario(feed, w, bot, storage):
        feed.append(post(2, "Графік ГПВ на 8 жовтня.\n" + BODY, 6))  # учорашній — не шлемо
        v1 = change(BODY, "2.1 09:00 - 11:00", "2.1 09:00 - 10:00")
        v2 = change(BODY, "2.1 09:00 - 11:00", "2.1 09:00 - 12:00")
        feed.append(post(3, "Оновлений графік ГПВ на 9 жовтня.\n" + v1, 7))
        feed.append(post(4, "Оновлений графік ГПВ на 9 жовтня.\n" + v2, 7))
        await w.check_once(None)
        assert len(bot.sent) == 1 and "2.1</b>  09:00–12:00" in bot.sent[0]

    run(tmp_path, monkeypatch, scenario)


def test_edit_of_outdated_post_is_ignored(tmp_path, monkeypatch):
    async def scenario(feed, w, bot, storage):
        a = post(2, "Графік ГПВ на 10 жовтня.\n" + BODY, 8)
        feed.append(a)
        await w.check_once(None)
        b_body = change(BODY, "4.2 07:00 - 09:00", "4.2 08:00 - 09:00")
        feed.append(post(3, "Оновлений графік ГПВ на 10 жовтня.\n" + b_body, 10))
        await w.check_once(None)
        assert len(bot.sent) == 2

        # обленерго редагує СТАРИЙ пост на 10-те — актуальний усе одно пост 3
        a.text = a.text.replace("5.1 09:00 - 11:00", "5.1 09:00 - 10:00")
        await w.check_once(None)
        assert len(bot.sent) == 2

        # а правка НАЙНОВІШОГО посту на 9-те (пост 1) — це справжнє оновлення
        feed[0].text = feed[0].text.replace("20:00 - 22:00", "20:00 - 23:00")  # 6.1
        await w.check_once(None)
        assert len(bot.sent) == 3 and "9 жовтня" in bot.sent[-1] and "6.1</b>  01:00–02:00 · 14:00–16:00 · 20:00–23:00" in bot.sent[-1]

        current = await publisher.current_schedules(storage)
        assert [u for _, u in current] == ["https://t.me/x/1", "https://t.me/x/3"]

    run(tmp_path, monkeypatch, scenario)


def test_real_update_ignores_past_hours():
    """Реальні пости 9 жовтня: о 12:55 обленерго прибрало минулі інтервали і змінило лише 4.1."""
    import json
    from pathlib import Path

    from bot.formatter import format_caption
    from bot.render import render_schedule

    data = json.loads((Path(__file__).parent / "real_update_9oct.json").read_text(encoding="utf-8"))
    old = publisher._parse(data["1807"]["text"], datetime.fromisoformat(data["1807"]["posted_at"]))
    new = publisher._parse(data["1809"]["text"], datetime.fromisoformat(data["1809"]["posted_at"]))
    new.mark_changes(old)
    assert new.cutoff == 12 * 60 + 55
    assert new.changed == {"4.1"}
    assert new.added("4.1") == [(780, 900)] and new.removed("4.1") == []
    cap = format_caption(new, "u")
    assert cap.count("✏️") == 2 and "було: 17:00–19:00" in cap and "від 01:09" in cap
    render_schedule(new)


def test_update_that_only_drops_past_is_not_sent(tmp_path, monkeypatch):
    async def scenario(feed, w, bot, storage):
        # о 12:30 (09:30 UTC) прибрали інтервали, що вже минули, — по суті нічого не змінилось
        future = """
1.1 15:00 - 17:00, 21:00 - 23:00
1.2 15:00 - 17:00, 21:00 - 23:00
2.1 17:00 - 19:00
2.2 16:00 - 18:00, 23:00 - 00:00
3.1 13:00 - 15:00, 19:00 - 21:00
3.2 12:00 - 14:00, 19:00 - 21:00
4.1 17:00 - 19:00
4.2 15:00 - 17:00, 22:00 - 24:00
5.1 17:00 - 19:00, 23:00 - 24:00
5.2 19:00 - 21:00
6.1 14:00 - 16:00, 20:00 - 22:00
6.2 11:00 - 13:00, 18:00 - 20:00
"""
        feed.append(Post(2, "Оновлений графік ГПВ на 9 жовтня.\n" + future,
                         datetime(2026, 10, 9, 9, 30, tzinfo=timezone.utc), "https://t.me/x/2"))
        await w.check_once(None)
        assert bot.sent == []

        # а реальна зміна в майбутньому — надсилається, і змінена лише одна черга
        changed = change(future, "6.2 11:00 - 13:00, 18:00", "6.2 11:00 - 13:00, 17:00")
        feed.append(Post(3, "Оновлений графік ГПВ на 9 жовтня.\n" + changed,
                         datetime(2026, 10, 9, 9, 40, tzinfo=timezone.utc), "https://t.me/x/3"))
        await w.check_once(None)
        assert len(bot.sent) == 1
        assert bot.sent[0].count("✏️") == 2 and "було: 11:00–13:00 · 18:00–20:00" in bot.sent[0]

    run(tmp_path, monkeypatch, scenario)


def test_removed_hours_reported():
    from bot.formatter import format_caption
    from bot.parser import parse_post

    old = parse_post("Графік ГПВ на 10 жовтня.\n" + BODY, NOW.date())
    new = parse_post("Оновлений графік ГПВ на 10 жовтня.\n" + change(BODY, "2.1 09:00 - 11:00, ", "2.1 "), NOW.date())
    new.mark_changes(old)  # графік на завтра — cutoff 0, порівнюється весь день
    assert new.cutoff == 0 and new.changed == {"2.1"} and new.removed("2.1") == [(540, 660)]
    assert "2.1</b>  17:00–19:00  ✏️\n      ↳ <i>було: 09:00–11:00 · 17:00–19:00</i>" in format_caption(new, "u")


def test_render_interval_ending_minutes_after_update():
    """Оновлення о 16:57, відключення 16:30–17:00: раніше картинка падала на відрізку ~1 px."""
    from bot.formatter import format_caption
    from bot.parser import parse_post
    from bot.render import render_schedule

    old = parse_post("Графік ГПВ на 9 жовтня.\n" + BODY, NOW.date())
    new = parse_post("Оновлений графік ГПВ на 9 жовтня.\n"
                     + change(BODY, "2.1 09:00 - 11:00, 17:00 - 19:00", "2.1 16:30 - 17:00, 17:59 - 18:00"),
                     NOW.date())
    new.published = datetime(2026, 10, 9, 16, 57, tzinfo=publisher.KYIV)
    new.mark_changes(old)
    assert new.added("2.1") == [(990, 1020)]  # увесь новий відрізок 16:30–17:00, а не 16:57–17:00
    assert new.removed("2.1") == [(1020, 1079), (1080, 1140)]
    assert "було: 17:00–19:00" in format_caption(new, "u")
    render_schedule(new)


def test_text_fallback_when_render_fails(monkeypatch):
    sent = []

    class Bot:
        async def send_message(self, chat_id, text, parse_mode, disable_web_page_preview):
            sent.append(("text", chat_id, text))

    def boom(schedule):
        raise ValueError("render failed")

    from bot.parser import parse_post
    monkeypatch.setattr(publisher, "render_schedule", boom)
    schedule = parse_post("Графік ГПВ на 9 жовтня.\n" + BODY, NOW.date())
    png, caption = publisher.build_message(schedule, "u")
    assert png is None and "9 жовтня" in caption
    assert asyncio.run(publisher.send_schedule(Bot(), None, 1, png, caption))
    assert sent and sent[0][0] == "text"
