from datetime import date
from pathlib import Path

from bot.formatter import format_caption
from bot.parser import QUEUES, parse_post
from bot.render import render_schedule
from bot.scraper import parse_channel_html

SAMPLE = """Через постійні ворожі обстріли та наслідки попередніх ракетно-дронових атак за розпорядженням НЕК «Укренерго» 9 жовтня з 00:00 до 24:00 у Черкаській області будуть застосовані графіки погодинних відключень (ГПВ).

Години відсутності електропостачання:

1.1 05:00 - 07:00, 15:00 - 17:00, 21:00 - 23:00

1.2 05:00 - 07:00, 15:00 - 17:00, 21:00 - 23:00

2.1 09:00 - 11:00, 17:00 - 19:00

2.2 07:00 - 09:00, 16:00 - 18:00, 23:00 - 00:00

3.1 03:00 - 05:00, 13:00 - 15:00, 19:00 - 21:00

3.2 01:00 - 03:00, 12:00 - 14:00, 19:00 - 21:00

4.1 08:00 - 10:00, 17:00 - 19:00

4.2 07:00 - 09:00, 15:00 - 17:00, 22:00 - 24:00

5.1 09:00 - 11:00, 17:00 - 19:00, 23:00 - 24:00

5.2 00:00 - 01:00, 10:00 - 12:00, 19:00 - 21:00

6.1 01:00 - 02:00, 14:00 - 16:00, 20:00 - 22:00

6.2 00:00 - 01:00, 11:00 - 13:00, 18:00 - 20:00

Свою чергу/підчергу можна дізнатися у чат-ботах, розділ "Відключення"-"Черги ГПВ/ГАВ":
"""


def test_parse_sample():
    s = parse_post(SAMPLE, date(2026, 10, 8))
    assert s is not None
    assert s.day == date(2026, 10, 9)
    assert not s.updated
    assert set(s.queues) == set(QUEUES)
    assert s.queues["1.1"] == [(300, 420), (900, 1020), (1260, 1380)]
    assert s.queues["2.2"][-1] == (1380, 1440)  # 23:00 - 00:00
    assert s.queues["4.2"][-1] == (1320, 1440)  # 22:00 - 24:00
    assert s.queues["5.2"][0] == (0, 60)
    assert s.off_minutes("2.1") == 240


def test_non_schedule_post_ignored():
    text = 'Для промисловості 9 жовтня будуть застосовані графіки обмеження потужності (ГОП).'
    assert parse_post(text, date(2026, 10, 8)) is None


def test_updated_flag_and_date():
    text = "Оновлений графік погодинних відключень (ГПВ) на 8 жовтня.\n\n" + SAMPLE.split("електропостачання:")[1]
    s = parse_post(text, date(2026, 10, 8))
    assert s.updated and s.day == date(2026, 10, 8)


def test_caption_and_render():
    s = parse_post(SAMPLE, date(2026, 10, 8))
    cap = format_caption(s, "https://t.me/pat_cherkasyoblenergo/1806")
    assert "9 жовтня" in cap and len(cap) <= 1024
    assert "05:00–07:00 · 15:00–17:00 · 21:00–23:00" in cap
    png = render_schedule(s)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_scrape_sample_html():
    html = (Path(__file__).parent / "channel_sample.html").read_text(encoding="utf-8")
    posts = parse_channel_html(html)
    assert posts and posts == sorted(posts, key=lambda p: p.post_id)
    schedules = [p for p in posts if parse_post(p.text, p.posted_at.date())]
    assert schedules, "у зразку мають бути пости з графіком"


def test_half_hours():
    text = SAMPLE.replace("2.1 09:00 - 11:00, 17:00 - 19:00", "2.1 07:30 - 09:30, 17:00 - 18:30")
    s = parse_post(text, date(2026, 10, 8))
    assert s.queues["2.1"] == [(450, 570), (1020, 1110)]
    assert s.off_minutes("2.1") == 210
    cap = format_caption(s, "u")
    assert "07:30–09:30 · 17:00–18:30" in cap
    render_schedule(s)


def test_mark_changes():
    old = parse_post(SAMPLE, date(2026, 10, 8))
    new_text = "Оновлений графік ГПВ на 9 жовтня.\n" + SAMPLE.replace(
        "1.1 05:00 - 07:00, 15:00 - 17:00, 21:00 - 23:00", "1.1 05:00 - 08:00, 15:00 - 17:00")
    new = parse_post(new_text, date(2026, 10, 9))
    new.mark_changes(old)
    assert new.changed == {"1.1"} and new.updated and new.has_previous
    cap = format_caption(new, "u")
    assert "1.1</b>  05:00–08:00 · 15:00–17:00  ✏️" in cap


def test_real_11oct_dots_after_queue():
    """Реальний пост на 11 жовтня: «5.2. 17:00 - 19:00» і «6.2. 18:00 - 20:00» з крапкою після номера."""
    text = (Path(__file__).parent / "real_11oct.txt").read_text(encoding="utf-8")
    s = parse_post(text, date(2026, 10, 10))
    assert s.day == date(2026, 10, 11)
    assert s.queues["5.2"] == [(1020, 1140)]
    assert s.queues["6.2"] == [(1080, 1200)]
    assert s.queues["2.1"] == []  # 2.1 у пості справді немає
    assert s.queues["4.1"] == [(1320, 1440)]


def test_queue_line_variants():
    variants = ["5.2 17:00 - 19:00", "5.2. 17:00 - 19:00", "5,2 17:00-19:00", "5.2: 17:00 – 19:00",
                "5.2 - 17:00 - 19:00", "5.2) 17:00 - 19:00", "Черга 5.2 17:00 - 19:00", "  5 . 2   17.00 - 19.00"]
    for v in variants:
        text = SAMPLE.replace("5.2 00:00 - 01:00, 10:00 - 12:00, 19:00 - 21:00", v)
        s = parse_post(text, date(2026, 10, 8))
        assert s.queues["5.2"] == [(1020, 1140)], v
        assert s.queues["5.1"] == [(540, 660), (1020, 1140), (1380, 1440)], v  # сусіди не постраждали


def test_unrecognized_line_is_logged(caplog):
    text = SAMPLE.replace("5.2 00:00", "5.3 00:00")
    with caplog.at_level("WARNING"):
        parse_post(text, date(2026, 10, 8))
    assert "Не розпізнано рядок графіка" in caplog.text and "5.3" in caplog.text
