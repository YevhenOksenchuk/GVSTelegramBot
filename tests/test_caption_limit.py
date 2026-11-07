"""Підпис до картинки завжди вміщається в ліміт Telegram (1024 символи UTF-16)."""
import asyncio
from datetime import date, datetime
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest

from bot import publisher
from bot.formatter import CAPTION_LIMIT, format_caption, hard_trim, tg_len
from bot.parser import QUEUES, parse_post
from bot.publisher import KYIV
from bot.storage import Storage
from tests.test_dates import BODY

# Реальний випадок 8 листопада: по 5 інтервалів у кожній черзі
HEAVY = "\n\n".join(
    f"{q} 00:30 - 02:00, 04:00 - 08:00, 10:00 - 14:00, 16:00 - 20:00, 22:00 - 24:00" for q in QUEUES
)
HEAVY_PREV = "\n\n".join(
    f"{q} 00:30 - 02:00, 05:00 - 07:00, 10:00 - 14:00, 16:00 - 20:00, 22:00 - 24:00" for q in QUEUES
)


def heavy_update():
    prev = parse_post("Графік ГПВ на 8 листопада.\n" + HEAVY_PREV, date(2026, 11, 7))
    prev.published = datetime(2026, 11, 7, 23, 27, tzinfo=KYIV)
    upd = parse_post("Оновлено графік ГПВ на 8 листопада.\n" + HEAVY, date(2026, 11, 8))
    upd.published = datetime(2026, 11, 8, 0, 40, tzinfo=KYIV)
    upd.mark_changes(prev)
    assert len(upd.changed) == 12
    return upd


def test_tg_len_counts_emoji_as_two():
    assert tg_len("<b>ab</b>") == 2 and tg_len("🔴 1.1") == 6 and tg_len("&amp;") == 1  # емодзі = 2


# Точні рядки реальних постів 1981 (23:27) і 1982 (00:40) на 8 листопада — 9 змінених черг
REAL = """1.1 00:00 - 02:00, 04:00 - 08:00, 10:00 - 14:00, 16:00 - 20:00, 22:00 - 24:00

1.2 00:00 - 03:00, 05:00 - 08:00, 11:00 - 15:00, 17:00 - 21:00, 23:00 - 24:00

2.1 00:00 - 02:00, 04:00 - 08:00, 10:00 - 14:00, 16:00 - 20:00, 23:00 - 24:00

2.2 00:00 - 03:00, 05:00 - 09:00, 11:00 - 15:00, 17:00 - 21:00, 23:00 - 24:00

3.1 01:00 - 04:00, 07:00 - 10:00, 12:00 - 16:00, 18:00 - 22:00

3.2 01:00 - 04:00, 08:00 - 11:00, 13:00 - 17:00, 19:00 - 22:00

4.1 00:00 - 03:00, 06:00 - 10:00, 12:00 - 16:00, 18:00 - 22:00

4.2 02:00 - 05:00, 07:00 - 11:00, 13:00 - 17:00, 19:00 - 23:00

5.1 00:00 - 01:00, 03:00 - 06:00, 08:00 - 12:00, 14:00 - 18:00, 20:00 - 23:00

5.2 00:00 - 01:00, 03:00 - 07:00, 09:00 - 13:00, 15:00 - 19:00, 21:00 - 23:00

6.1 02:00 - 05:00, 08:00 - 12:00, 14:00 - 18:00, 20:00 - 24:00

6.2 00:00 - 01:00, 03:00 - 07:00, 09:00 - 13:00, 15:00 - 19:00, 21:00 - 24:00"""
REAL_PREV = (REAL.replace("1.1 00:00 - 02:00, 04:00 - 08:00", "1.1 00:00 - 02:00, 05:00 - 07:00")
             .replace("1.2 00:00 - 03:00, 05:00 - 08:00", "1.2 00:00 - 03:00, 06:00 - 08:00")
             .replace("2.1 00:00 - 02:00, 04:00 - 08:00", "2.1 00:00 - 02:00, 05:00 - 08:00")
             .replace("2.2 00:00 - 03:00, 05:00 - 09:00", "2.2 00:00 - 03:00, 05:00 - 08:00")
             .replace("3.1 01:00 - 04:00, 07:00", "3.1 07:00").replace("3.2 01:00 - 04:00, 08:00", "3.2 08:00")
             .replace("5.1 00:00 - 01:00, 03:00 - 06:00", "5.1 00:00 - 01:00, 03:00 - 05:00")
             .replace("5.2 00:00 - 01:00, 03:00 - 07:00", "5.2 00:00 - 01:00, 03:00 - 06:00")
             .replace("6.2 00:00 - 01:00, 03:00 - 07:00", "6.2 00:00 - 01:00, 03:00 - 06:00"))


def real_update():
    prev = parse_post("Графік ГПВ на 8 листопада.\n" + REAL_PREV, date(2026, 11, 7))
    prev.published = datetime(2026, 11, 7, 23, 27, tzinfo=KYIV)
    upd = parse_post("Оновлено графік ГПВ на 8 листопада.\n" + REAL, date(2026, 11, 8))
    upd.published = datetime(2026, 11, 8, 0, 40, tzinfo=KYIV)
    upd.mark_changes(prev)
    assert len(upd.changed) == 9
    return upd


def test_real_update_keeps_full_time_format_but_drops_hint():
    upd = real_update()
    cap = format_caption(upd, "https://t.me/x/1982")
    assert tg_len(cap) <= CAPTION_LIMIT
    assert "04:00–08:00" in cap and "Своя черга" not in cap and "було:" not in cap  # час повний, підказку прибрано
    personal = format_caption(upd, "https://t.me/x/1982", my_queue="4.1")
    assert tg_len(personal) <= CAPTION_LIMIT and "04:00–08:00" in personal
    assert "Ваша 4.1" not in personal  # у важкий день рядок «Ваша черга» жертвуємо раніше за формат часу


def test_heavy_update_caption_fits_and_keeps_all_queues():
    upd = heavy_update()
    for my_queue in (None, "4.1"):
        cap = format_caption(upd, "https://t.me/x/1982", my_queue=my_queue)
        assert tg_len(cap) <= CAPTION_LIMIT, tg_len(cap)
        assert all(f"<b>{q}</b>" in cap for q in QUEUES) and "Джерело" in cap
        assert "00:30–02" in cap  # найважчий випадок: компактний час («:30» лишається)
        assert "було:" not in cap


def test_normal_schedule_keeps_full_format():
    s = parse_post("Графік ГПВ на 9 жовтня.\n" + BODY, date(2026, 10, 9))
    cap = format_caption(s, "u")
    assert "05:00–07:00" in cap and "Своя черга" in cap and tg_len(cap) <= CAPTION_LIMIT


def test_hard_trim_keeps_source_line():
    text = "\n".join(["🔴 рядок " + "x" * 60] * 30 + ['<a href="u">Джерело</a>'])
    trimmed = hard_trim(text, 400)
    assert tg_len(trimmed) <= 400 and trimmed.endswith('…\n<a href="u">Джерело</a>')


def test_send_schedule_retries_with_trimmed_caption(tmp_path, monkeypatch):
    sent = []

    class Bot:
        async def send_photo(self, chat_id, photo, caption, parse_mode):
            if tg_len(caption) > CAPTION_LIMIT:
                raise TelegramBadRequest(method=None, message="Bad Request: message caption is too long")
            sent.append(caption)
            return SimpleNamespace(message_id=1)

    async def main():
        storage = Storage(str(tmp_path / "t.db"))
        await storage.open()
        try:
            long_caption = "\n".join(["🔴 1.1  00:00–02:00 · 04:00–08:00"] * 60 + ['<a href="u">Джерело</a>'])
            assert tg_len(long_caption) > CAPTION_LIMIT
            assert await publisher.send_schedule(Bot(), storage, 5, b"png", long_caption) == 1
            assert len(sent) == 1 and tg_len(sent[0]) <= CAPTION_LIMIT and sent[0].endswith("Джерело</a>")
        finally:
            await storage.close()

    asyncio.run(main())
