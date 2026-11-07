"""Текстовий підпис до картинки (HTML для Telegram)."""
from __future__ import annotations

import re
from html import escape, unescape
from typing import List, Optional

from .parser import QUEUES, Interval, Schedule

CAPTION_LIMIT = 1024  # підпис до фото; Telegram рахує символи в UTF-16 (емодзі = 2)
TEXT_LIMIT = 4096     # звичайне повідомлення
HINT = 'Своя черга: <a href="https://www.cherkasyoblenergo.com/off">cherkasyoblenergo.com/off</a>'


def _hhmm(minute: int, compact: bool = False) -> str:
    h, m = divmod(minute, 60)
    return f"{h:02d}" if compact and m == 0 else f"{h:02d}:{m:02d}"


def _intervals(items: List[Interval], compact: bool = False) -> str:
    if not items:
        return "без відключень"
    return " · ".join(f"{_hhmm(s, compact)}–{_hhmm(e, compact)}" for s, e in items)


def tg_len(html: str) -> int:
    """Довжина тексту так, як її рахує Telegram: без тегів, у UTF-16 (емодзі — по 2)."""
    plain = unescape(re.sub(r"<[^>]+>", "", html))
    return len(plain.encode("utf-16-le")) // 2


def hard_trim(html: str, limit: int = CAPTION_LIMIT) -> str:
    """Крайній засіб: прибрати рядки з кінця (крім останнього — посилання на джерело), поки не вміститься."""
    lines = html.split("\n")
    tail = lines.pop() if len(lines) > 1 else ""
    while lines and tg_len("\n".join(lines + ["…", tail])) > limit:
        lines.pop()
    return "\n".join(lines + ["…", tail])


def _changes(schedule: Schedule, q: str, compact: bool = False) -> str:
    """Як було в попередньому графіку (без інтервалів, що вже минули до оновлення)."""
    was = [(s, e) for s, e in schedule.previous.queues.get(q, []) if e > schedule.cutoff]
    return f"      ↳ <i>було: {_intervals(was, compact)}</i>"


def _previous_label(schedule: Schedule) -> str:
    prev = schedule.previous.published if schedule.previous else None
    if not prev:
        return "попереднього графіка"
    if schedule.published and prev.date() == schedule.published.date():
        return f"графіка від {prev:%H:%M}"
    return f"графіка від {prev:%d.%m %H:%M}"


def format_caption(schedule: Schedule, source_url: str, my_queue: Optional[str] = None) -> str:
    """Підпис до картинки. my_queue — підчерга читача (в особистих): додає персональний рядок угорі.

    Telegram обмежує підпис 1024 символами, тому варіанти від повного до найкомпактнішого:
    з деталями «було:» → без деталей → години без «:00» → без підказки → без порожніх рядків → обрізання.
    """
    head = "🔄 <b>Оновлений графік ГПВ</b>" if schedule.updated else "⚡ <b>Графік відключень (ГПВ)</b>"
    if schedule.date_label:
        head += f" — <b>{escape(schedule.date_label)}</b>"

    def build(details: bool, compact: bool, hint: bool, gaps: bool) -> str:
        lines = [head]
        if schedule.updated and schedule.time_label:
            lines.append(f"🕐 оновлено о {schedule.time_label}")
        if my_queue:
            mark = "  ✏️" if my_queue in schedule.changed else ""
            lines += ["", f"💡 <b>Ваша {my_queue}:</b> {_intervals(schedule.queues.get(my_queue, []), compact)}{mark}"]
        lines.append("")
        for q in QUEUES:
            items = schedule.queues.get(q, [])
            mark = "  ✏️" if q in schedule.changed else ""
            lines.append(f"{'🔴' if items else '🔵'} <b>{q}</b>  {_intervals(items, compact)}{mark}")
            if details and q in schedule.changed:
                lines.append(_changes(schedule, q, compact))
            if gaps and q.endswith(".2") and q != QUEUES[-1]:
                lines.append("")
        if schedule.changed:
            lines += ["", f"✏️ — змінилось відносно {_previous_label(schedule)}"]
        elif schedule.has_previous:
            lines += ["", "Майбутні години без змін"]
        lines.append("")
        if hint:
            lines.append(HINT)
        lines.append(f'<a href="{escape(source_url)}">Джерело: Черкасиобленерго</a>')
        return "\n".join(lines)

    attempts = (
        (True, False, True, True),
        (False, False, True, True),
        (False, True, True, True),
        (False, True, False, True),
        (False, True, False, False),
    )
    text = ""
    for attempt in attempts:
        text = build(*attempt)
        if tg_len(text) <= CAPTION_LIMIT:
            return text
    return hard_trim(text)
