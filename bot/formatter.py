"""Текстовий підпис до картинки (HTML для Telegram)."""
from __future__ import annotations

import re
from html import escape, unescape
from typing import List, Optional

from .parser import QUEUES, Interval, Schedule

CAPTION_LIMIT = 1024


def _hhmm(minute: int) -> str:
    h, m = divmod(minute, 60)
    return f"{h:02d}:{m:02d}"


def _intervals(items: List[Interval]) -> str:
    if not items:
        return "без відключень"
    return " · ".join(f"{_hhmm(s)}–{_hhmm(e)}" for s, e in items)


def _visible_len(html: str) -> int:
    return len(unescape(re.sub(r"<[^>]+>", "", html)))


def _changes(schedule: Schedule, q: str) -> str:
    """Як було в попередньому графіку (без інтервалів, що вже минули до оновлення)."""
    was = [(s, e) for s, e in schedule.previous.queues.get(q, []) if e > schedule.cutoff]
    return f"      ↳ <i>було: {_intervals(was)}</i>"


def _previous_label(schedule: Schedule) -> str:
    prev = schedule.previous.published if schedule.previous else None
    if not prev:
        return "попереднього графіка"
    if schedule.published and prev.date() == schedule.published.date():
        return f"графіка від {prev:%H:%M}"
    return f"графіка від {prev:%d.%m %H:%M}"


def format_caption(schedule: Schedule, source_url: str, my_queue: Optional[str] = None) -> str:
    """Підпис до картинки. my_queue — підчерга читача (в особистих): додає персональний рядок угорі."""
    head = "🔄 <b>Оновлений графік ГПВ</b>" if schedule.updated else "⚡ <b>Графік відключень (ГПВ)</b>"
    if schedule.date_label:
        head += f" — <b>{escape(schedule.date_label)}</b>"
    intro = [head]
    if schedule.updated and schedule.time_label:
        intro.append(f"🕐 оновлено о {schedule.time_label}")
    if my_queue:
        mark = "  ✏️" if my_queue in schedule.changed else ""
        intro += ["", f"💡 <b>Ваша {my_queue}:</b> {_intervals(schedule.queues.get(my_queue, []))}{mark}"]

    def build(with_details: bool) -> str:
        lines = intro + [""]
        for q in QUEUES:
            items = schedule.queues.get(q, [])
            mark = "  ✏️" if q in schedule.changed else ""
            lines.append(f"{'🔴' if items else '🔵'} <b>{q}</b>  {_intervals(items)}{mark}")
            if with_details and q in schedule.changed:
                lines.append(_changes(schedule, q))
            if q.endswith(".2") and q != QUEUES[-1]:
                lines.append("")
        if schedule.changed:
            lines += ["", f"✏️ — змінилось відносно {_previous_label(schedule)}"]
        elif schedule.has_previous:
            lines += ["", "Майбутні години без змін"]
        lines += [
            "",
            'Своя черга: <a href="https://www.cherkasyoblenergo.com/off">cherkasyoblenergo.com/off</a>',
            f'<a href="{escape(source_url)}">Джерело: Черкасиобленерго</a>',
        ]
        return "\n".join(lines)

    text = build(with_details=True)
    # Telegram обмежує підпис до фото 1024 видимими символами — якщо змін забагато, лише ✏️
    return text if _visible_len(text) <= CAPTION_LIMIT else build(with_details=False)
