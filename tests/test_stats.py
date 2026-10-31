"""Статистика за місяць: що реально діяло протягом дня, підсумки по чергах і днях, картинки."""
import asyncio
from datetime import date, datetime

from bot import publisher
from bot.publisher import KYIV
from bot.render import render_schedule
from bot.stats import effective_day, month_stats, render_month_stats
from bot.storage import Storage
from tests.test_dates import BODY
from tests.test_reminders import Q11, schedule_text

MORNING_GONE = "1.1 15:00 - 17:00, 20:00 - 22:00"  # оновлення вдень: ранкові години вже не пишуть


def sched(day, text, published):
    s = publisher._parse(text, published)
    assert s is not None and s.day == day
    return s


def test_effective_day_keeps_hours_before_update():
    v1 = sched(date(2026, 10, 9), schedule_text(9), datetime(2026, 10, 8, 21, 0, tzinfo=KYIV))
    v2 = sched(date(2026, 10, 9), schedule_text(9, MORNING_GONE, updated=True), datetime(2026, 10, 9, 12, 55, tzinfo=KYIV))
    late = sched(date(2026, 10, 9), schedule_text(9, "1.1 00:00 - 24:00"), datetime(2026, 10, 10, 9, 0, tzinfo=KYIV))
    eff = effective_day([v2, v1, late], date(2026, 10, 9))  # порядок на вході не важливий, «пізня» версія ігнорується
    assert eff["1.1"] == [(300, 420), (900, 1020), (1200, 1320)]  # 05–07 з v1, далі — з v2
    assert eff["2.1"] == [(540, 660), (1020, 1140)]  # у v2 те саме, що й у v1


def test_month_stats_range_and_sums():
    schedules = [
        sched(date(2026, 10, 8), schedule_text(8), datetime(2026, 10, 7, 20, 0, tzinfo=KYIV)),
        sched(date(2026, 10, 9), schedule_text(9), datetime(2026, 10, 8, 21, 0, tzinfo=KYIV)),
        sched(date(2026, 10, 9), schedule_text(9, MORNING_GONE, updated=True), datetime(2026, 10, 9, 12, 55, tzinfo=KYIV)),
        sched(date(2026, 10, 11), schedule_text(11, "1.1 06:00 - 07:30"), datetime(2026, 10, 10, 19, 0, tzinfo=KYIV)),
    ]
    st = month_stats(schedules, 2026, 10, date(2026, 10, 12))
    assert (st.first_day, st.last_day, st.days_covered) == (date(2026, 10, 8), date(2026, 10, 12), 5)
    assert st.days_with_outages == 3 and st.range_label == "8–12 жовтня"
    assert st.per_queue["1.1"] == 360 + 360 + 90  # 8-го повний BODY, 9-го з урахуванням оновлення, 11-го 1,5 год
    assert st.daily_avg(date(2026, 10, 10)) == 0  # день без графіка — без відключень
    november = month_stats(schedules, 2026, 11, date(2026, 11, 3))  # ще без відключень — нулі, а не «немає даних»
    assert (november.days_covered, november.days_with_outages, november.avg_minutes) == (3, 0, 0)
    assert month_stats(schedules, 2026, 9, date(2026, 10, 12)) is None  # до початку спостережень
    partial = month_stats(schedules, 2026, 10, date(2026, 10, 9))  # посеред місяця — по сьогодні
    assert partial.last_day == date(2026, 10, 9) and partial.per_queue["1.1"] == 720


def test_render_month_and_schedule_with_month():
    schedules = [sched(date(2026, 10, d), schedule_text(d), datetime(2026, 10, d - 1, 20, 0, tzinfo=KYIV))
                 for d in (8, 9, 10)]
    st = month_stats(schedules, 2026, 10, date(2026, 10, 10))
    png = render_month_stats(st)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    plain = render_schedule(schedules[-1])
    with_month = render_schedule(schedules[-1], st)
    assert with_month[:8] == b"\x89PNG\r\n\x1a\n" and len(with_month) != len(plain)


def test_month_for_uses_stored_posts(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "kyiv_now", lambda: datetime(2026, 10, 9, 12, 0, tzinfo=KYIV))

    async def scenario():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            await storage.save_post(1, schedule_text(8), "u1", datetime(2026, 10, 7, 20, 0, tzinfo=KYIV).isoformat(), True)
            await storage.save_post(2, schedule_text(9), "u2", datetime(2026, 10, 8, 21, 0, tzinfo=KYIV).isoformat(), True)
            await storage.save_post(3, "Новини без графіка", "u3", datetime(2026, 10, 8, 22, 0, tzinfo=KYIV).isoformat(), False)
            schedule, _ = await publisher.latest_schedule_for_day(storage, date(2026, 10, 9))
            st = await publisher.month_for(storage, schedule)
            assert st.per_queue["1.1"] == 720 and st.range_label == "7–9 жовтня"  # дані з найранішого поста (7-го)
            tomorrow = sched(date(2026, 11, 1), schedule_text(1).replace("жовтня", "листопада"),
                             datetime(2026, 10, 31, 20, 0, tzinfo=KYIV))
            await storage.save_post(4, tomorrow_text := schedule_text(1).replace("жовтня", "листопада"), "u4",
                                    datetime(2026, 10, 31, 20, 0, tzinfo=KYIV).isoformat(), True)
            st = await publisher.month_for(storage, tomorrow)  # графік на 1 листопада — місяць листопад, 1 день
            assert st.month == 11 and st.days_covered == 1 and st.per_queue["1.1"] == 360
        finally:
            await storage.close()

    asyncio.run(scenario())


def test_month_stats_with_data_start_counts_quiet_days():
    schedules = [sched(date(2026, 10, d), schedule_text(d), datetime(2026, 10, d - 1, 20, 0, tzinfo=KYIV))
                 for d in (6, 7)]
    st = month_stats(schedules, 2026, 10, date(2026, 10, 7), data_start=date(2026, 9, 11))
    assert (st.first_day, st.days_covered, st.days_with_outages) == (date(2026, 10, 1), 7, 2)
    assert st.since_label == "" and st.range_label == "1–7 жовтня"
    later = month_stats(schedules, 2026, 10, date(2026, 10, 7), data_start=date(2026, 10, 3))
    assert later.first_day == date(2026, 10, 3) and later.since_label == " (з 3-го)"
    assert month_stats([], 2026, 10, date(2026, 10, 7), data_start=date(2026, 10, 1)).days_with_outages == 0


def test_backfill_fetches_older_pages(tmp_path, monkeypatch):
    from bot.scraper import Post

    pages = {  # «до» якого поста запитали -> сторінка старіших постів
        1800: [Post(1795, "Новини", datetime(2026, 10, 4, 9, 0, tzinfo=KYIV), "u1795"),
               Post(1798, schedule_text(6), datetime(2026, 10, 5, 20, 0, tzinfo=KYIV), "u1798")],
        1795: [Post(1780, "Ще новини", datetime(2026, 9, 25, 9, 0, tzinfo=KYIV), "u1780"),
               Post(1790, "Новини", datetime(2026, 10, 1, 9, 0, tzinfo=KYIV), "u1790")],
        1780: [Post(1770, "Серпень", datetime(2026, 8, 20, 9, 0, tzinfo=KYIV), "u1770")],
    }
    calls = []

    async def fake_fetch(client, channel, before=None):
        calls.append(before)
        return pages.get(before, [])

    monkeypatch.setattr(publisher, "fetch_posts", fake_fetch)
    monkeypatch.setattr(publisher, "kyiv_now", lambda: datetime(2026, 10, 31, 12, 0, tzinfo=KYIV))

    async def scenario():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            await storage.save_post(1800, schedule_text(8), "u1800", datetime(2026, 10, 7, 20, 0, tzinfo=KYIV).isoformat(), True)
            w = publisher.Watcher(None, storage, "x", 60)
            await w.ensure_history(None)
            assert calls == [1800, 1795, 1780]  # вантажимо, поки не дійдемо до 1 вересня (попередній місяць)
            assert (await storage.earliest_post_date()).startswith("2026-08-20")
            assert [pid for pid, *_ in await storage.all_schedule_posts()] == [1798, 1800]
            assert await storage.get_meta("history_backfilled")
            calls.clear()
            await w.ensure_history(None)  # вдруге не вантажить
            assert calls == []
        finally:
            await storage.close()

    asyncio.run(scenario())
