"""Розбір тексту посту Черкасиобленерго у структурований графік."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Set, Tuple

MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}
MONTHS_BY_NUM = {v: k for k, v in MONTHS.items()}

log = logging.getLogger(__name__)

QUEUES = [f"{q}.{s}" for q in range(1, 7) for s in (1, 2)]
MIN_QUEUES = 6

# Інтервал у хвилинах від початку доби: (start, end), end може бути 1440.
Interval = Tuple[int, int]

# Рядок підчерги: «5.2 17:00 - 19:00», а також «5.2. …», «5,2 …», «5.2: …», «5.2 - …», «Черга 5.2 …»
_QUEUE_LINE = re.compile(
    r"^[ \t]*(?:(?:під)?черга[ \t]*)?([1-6])[ \t]*[.,][ \t]*([12])(?!\d)[ \t.,:;)\-–—]*(.*?)[ \t]*$",
    re.M | re.I,
)
_RANGE = re.compile(r"(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})")
_DATE = re.compile(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")", re.I)
_NO_POWER_WORDS = ("відключен", "відсутності електропостачання", "гпв")


@dataclass
class Schedule:
    day: Optional[date]
    queues: Dict[str, List[Interval]] = field(default_factory=dict)
    updated: bool = False  # «Оновлений графік»
    changed: Set[str] = field(default_factory=set)  # підчерги, що змінились відносно попереднього
    published: Optional[datetime] = None  # час публікації посту (Київ)
    has_previous: bool = False  # чи був раніше графік на цю ж дату
    previous: Optional["Schedule"] = None  # попередня версія графіка на цю ж дату
    post_id: Optional[int] = None  # пост обленерго, з якого розібрано графік

    def off_minutes(self, queue: str) -> int:
        return sum(e - s for s, e in self.queues.get(queue, []))

    @property
    def date_label(self) -> str:
        return date_label(self.day)

    @property
    def time_label(self) -> str:
        return self.published.strftime("%H:%M") if self.published else ""

    @property
    def cutoff(self) -> int:
        """Хвилина доби, до якої графік уже минув.

        Оновлення на сьогодні обленерго публікує без інтервалів, що вже закінчились,
        тому все до моменту публікації не порівнюємо і показуємо як минуле.
        """
        if self.updated and self.published and self.day == self.published.date():
            return self.published.hour * 60 + self.published.minute
        return 0

    def added(self, queue: str) -> List[Interval]:
        """Нові години відключень, яких не було в попередньому графіку (лише ті, що ще не минули)."""
        if not self.previous:
            return []
        return self._not_past(subtract(self.queues.get(queue, []), self.previous.queues.get(queue, [])))

    def removed(self, queue: str) -> List[Interval]:
        """Скасовані години відключень (лише ті, що ще не минули)."""
        if not self.previous:
            return []
        return self._not_past(subtract(self.previous.queues.get(queue, []), self.queues.get(queue, [])))

    def _not_past(self, intervals: List[Interval]) -> List[Interval]:
        # Відрізок, що вже закінчився до оновлення, — не зміна: обленерго просто прибрало минуле.
        return [(s, e) for s, e in intervals if e > self.cutoff]

    def past_from_previous(self, queue: str) -> List[Interval]:
        """Відключення до cutoff за попереднім графіком (яких уже немає в новому тексті)."""
        if not self.previous or not self.cutoff:
            return []
        return clip(self.previous.queues.get(queue, []), 0, self.cutoff)

    def mark_changes(self, previous: "Schedule") -> None:
        self.updated = True
        self.has_previous = True
        self.previous = previous
        self.changed = {q for q in QUEUES if self.added(q) or self.removed(q)}


def date_label(day: Optional[date]) -> str:
    """9 жовтня"""
    return f"{day.day} {MONTHS_BY_NUM[day.month]}" if day else ""


def clip(intervals: List[Interval], start: int, end: int = 1440) -> List[Interval]:
    """Частини інтервалів у межах [start, end)."""
    return [(max(s, start), min(e, end)) for s, e in intervals if min(e, end) > max(s, start)]


def subtract(a: List[Interval], b: List[Interval]) -> List[Interval]:
    """a мінус b."""
    result: List[Interval] = []
    for s, e in a:
        pieces = [(s, e)]
        for bs, be in b:
            pieces = [p for ps, pe in pieces for p in ((ps, min(pe, bs)), (max(ps, be), pe)) if p[1] > p[0]]
        result.extend(pieces)
    return result


def _to_minutes(h: str, m: str) -> int:
    return int(h) * 60 + int(m)


def parse_intervals(text: str) -> List[Interval]:
    result: List[Interval] = []
    for h1, m1, h2, m2 in _RANGE.findall(text):
        start, end = _to_minutes(h1, m1), _to_minutes(h2, m2)
        if end == 0 or end > 1440:  # «23:00 - 00:00» або «24:00» = кінець доби
            end = 1440
        if start >= 1440 or end <= start:
            continue
        result.append((start, end))
    return _merge(result)


def _merge(intervals: List[Interval]) -> List[Interval]:
    merged: List[Interval] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def parse_date(text: str, fallback: Optional[date] = None) -> Optional[date]:
    m = _DATE.search(text)
    if not m:
        return fallback
    day, month = int(m.group(1)), MONTHS[m.group(2).lower()]
    year = fallback.year if fallback else date.today().year
    # Графік на 1 січня, опублікований 31 грудня.
    if fallback and fallback.month == 12 and month == 1:
        year += 1
    try:
        return date(year, month, day)
    except ValueError:
        return fallback


def unrecognized_lines(text: str) -> List[str]:
    """Рядки з часом, які не вдалося прив'язати до підчерги, — ознака нового формату посту."""
    matched = {m.group(0).strip() for m in _QUEUE_LINE.finditer(text) if parse_intervals(m.group(3))}
    result: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line and line[0].isdigit() and _RANGE.search(line) and line not in matched:
            result.append(line)
    return result


def parse_post(text: str, posted: Optional[date] = None) -> Optional[Schedule]:
    """Повертає Schedule, якщо пост схожий на графік ГПВ, інакше None."""
    lowered = text.lower()
    if not any(w in lowered for w in _NO_POWER_WORDS):
        return None
    queues: Dict[str, List[Interval]] = {}
    for m in _QUEUE_LINE.finditer(text):
        queue, rest = f"{m.group(1)}.{m.group(2)}", m.group(3)
        intervals = parse_intervals(rest)
        if intervals or re.search(r"(не\s+відключа|без\s+відключ|світло\s+є)", rest, re.I):
            queues.setdefault(queue, []).extend(intervals)
    for line in unrecognized_lines(text):
        log.warning("Не розпізнано рядок графіка: %r", line)
    if len(queues) < MIN_QUEUES:
        return None
    for q in QUEUES:
        queues[q] = _merge(queues.get(q, []))
    return Schedule(
        day=parse_date(text, posted),
        queues=queues,
        updated="оновлен" in lowered,
    )
