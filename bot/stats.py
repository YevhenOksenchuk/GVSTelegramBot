"""Статистика: скільки годин без світла за графіками ГПВ — по чергах за місяць (або за весь час) і по днях.

Обленерго публікує кілька версій графіка на день, і оновлення не містять годин, що вже минули.
Тому рахуємо «що реально діяло протягом дня»: до моменту публікації оновлення — попередня версія,
після — нова. Рахуються лише планові відключення (ГПВ); аварійні (ГАВ) у графіках не публікуються.
"""
from __future__ import annotations

import calendar
import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw

from .parser import MONTHS_BY_NUM, QUEUES, Interval, Schedule, _merge, clip
from .render import BG, CARD, GRID, MUTED, OFF, PAD, SUBTLE, TEXT, W, _font

MONTH_LOC = {1: "січні", 2: "лютому", 3: "березні", 4: "квітні", 5: "травні", 6: "червні", 7: "липні",
             8: "серпні", 9: "вересні", 10: "жовтні", 11: "листопаді", 12: "грудні"}
MONTH_NOM = {1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень", 5: "Травень", 6: "Червень", 7: "Липень",
             8: "Серпень", 9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень"}


def effective_day(versions: List[Schedule], day: date) -> Dict[str, List[Interval]]:
    """Відключення, що реально діяли протягом дня, з урахуванням усіх оновлень графіка."""
    effective: Dict[str, List[Interval]] = {q: [] for q in QUEUES}
    for version in sorted(versions, key=lambda s: s.published):
        published = version.published
        if published.date() > day:
            continue  # версія з'явилась уже після цього дня — не стосується
        cutoff = published.hour * 60 + published.minute if published.date() == day else 0
        for q in QUEUES:
            effective[q] = _merge(clip(effective[q], 0, cutoff) + clip(version.queues.get(q, []), cutoff, 1440))
    return effective


def _day_label(day: date) -> str:
    return f"{day.day} {MONTHS_BY_NUM[day.month]}"


@dataclass
class MonthStats:
    """Підсумок за період: місяць (month=1..12) або весь час спостережень (month=0)."""
    year: int
    month: int
    first_day: date                      # з якого дня пораховано
    last_day: date                       # по який день (сьогодні або кінець місяця)
    per_queue: Dict[str, int]            # хвилин без світла за період
    per_day: Dict[date, Dict[str, int]]  # хвилин по днях і чергах (лише дні, коли був графік)
    axis_start: date                     # діапазон осі «по днях» на картинці
    axis_end: date

    @property
    def is_month(self) -> bool:
        return self.month != 0

    @property
    def title(self) -> str:
        return f"Без світла у {MONTH_LOC[self.month]}" if self.is_month else "Без світла за весь час"

    @property
    def period_word(self) -> str:
        return "за місяць" if self.is_month else "за період"

    @property
    def days_covered(self) -> int:
        return (self.last_day - self.first_day).days + 1

    @property
    def days_with_outages(self) -> int:
        return sum(1 for minutes in self.per_day.values() if any(minutes.values()))

    @property
    def avg_minutes(self) -> float:
        return sum(self.per_queue.values()) / len(QUEUES)

    @property
    def avg_per_day_hours(self) -> float:
        return self.avg_minutes / 60 / max(1, self.days_covered)

    def daily_avg(self, day: date) -> float:
        minutes = self.per_day.get(day)
        return sum(minutes.values()) / len(QUEUES) if minutes else 0.0

    @property
    def range_label(self) -> str:
        if self.first_day == self.last_day:
            return _day_label(self.first_day)
        if (self.first_day.year, self.first_day.month) == (self.last_day.year, self.last_day.month):
            return f"{self.first_day.day}–{self.last_day.day} {MONTHS_BY_NUM[self.first_day.month]}"
        return f"{_day_label(self.first_day)} – {_day_label(self.last_day)}"

    @property
    def since_label(self) -> str:
        """«(з 6-го)», якщо дані за місяць починаються не з 1-го числа; інакше порожньо."""
        return f" (з {self.first_day.day}-го)" if self.is_month and self.first_day.day != 1 else ""


def _period(schedules: List[Schedule], start: date, end: date) -> Tuple[Dict[str, int], Dict[date, Dict[str, int]]]:
    by_day: Dict[date, List[Schedule]] = defaultdict(list)
    for s in schedules:
        if s.day and s.published:
            by_day[s.day].append(s)
    per_queue = {q: 0 for q in QUEUES}
    per_day: Dict[date, Dict[str, int]] = {}
    day = start
    while day <= end:
        if day in by_day:
            effective = effective_day(by_day[day], day)
            per_day[day] = {q: sum(e - s for s, e in effective[q]) for q in QUEUES}
            for q in QUEUES:
                per_queue[q] += per_day[day][q]
        day += timedelta(days=1)
    return per_queue, per_day


def _known_from(schedules: List[Schedule], data_start: Optional[date]) -> Optional[date]:
    days = [s.day for s in schedules if s.day and s.published]
    candidates = [d for d in (data_start, min(days) if days else None) if d]
    return min(candidates) if candidates else None


def month_stats(schedules: List[Schedule], year: int, month: int, today: date,
                data_start: Optional[date] = None) -> Optional[MonthStats]:
    """data_start — з якої дати бот бачить канал (дні без графіка після неї = без відключень)."""
    known_from = _known_from(schedules, data_start)
    if known_from is None:
        return None
    month_start = date(year, month, 1)
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    start, end = max(month_start, known_from), min(today, month_end)  # раніше за перші відомі дані не рахуємо
    if start > end:
        return None
    per_queue, per_day = _period(schedules, start, end)
    return MonthStats(year, month, start, end, per_queue, per_day, month_start, month_end)


def all_time_stats(schedules: List[Schedule], today: date, data_start: Optional[date] = None) -> Optional[MonthStats]:
    known_from = _known_from(schedules, data_start)
    if known_from is None or known_from > today:
        return None
    per_queue, per_day = _period(schedules, known_from, today)
    return MonthStats(known_from.year, 0, known_from, today, per_queue, per_day, known_from, today)


# ---------------------------------------------------------------------------
# Картинка-підсумок
# ---------------------------------------------------------------------------

BAR_H = 22          # товщина смуги (тонкі позначки — дані, а не заливка)
ROW_H = 44
GROUP_GAP = 12
HAIRLINE = "#232B37"


def _hours(minutes: float) -> str:
    h = minutes / 60
    text = f"{h:.0f}" if abs(h - round(h)) < 0.05 else f"{h:.1f}".replace(".", ",")
    return f"{text} год"


def _per_day(minutes: float, days: int) -> str:
    return f"{minutes / 60 / max(days, 1):.1f}".replace(".", ",") + "/день"


def format_summary(st: MonthStats) -> str:
    """Підпис до картинки-підсумку (HTML)."""
    top_q = max(QUEUES, key=lambda q: st.per_queue[q])
    low_q = min(QUEUES, key=lambda q: st.per_queue[q])
    per_day = f"{st.avg_per_day_hours:.1f}".replace(".", ",")
    lines = [
        f"📊 <b>{st.title}</b> ({st.range_label})",
        f"У середньому <b>{_hours(st.avg_minutes)}</b> на чергу · {per_day} год/день",
        "",
        f"📈 Найбільше — {top_q} ({_hours(st.per_queue[top_q])})",
        "",
        f"📉 Найменше — {low_q} ({_hours(st.per_queue[low_q])})",
        "",
        f"📅 Днів з відключеннями: {st.days_with_outages} з {st.days_covered}",
        "",
        "<i>Лише планові відключення за графіками ГПВ.</i>",
    ]
    return "\n".join(lines)


def render_month_stats(st: MonthStats) -> bytes:
    axis_days = (st.axis_end - st.axis_start).days + 1
    header_h, kpi_h, section_gap = 150, 128, 56
    bars_h = len(QUEUES) * ROW_H + (len(QUEUES) // 2 - 1) * GROUP_GAP
    daily_h = 250
    footer_h = 70
    H = header_h + kpi_h + section_gap + 40 + bars_h + section_gap + 40 + daily_h + footer_h
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Заголовок
    d.text((PAD, PAD), st.title, font=_font(50, True), fill=TEXT)
    d.text((PAD, PAD + 100), f"за графіками ГПВ  ·  м. Черкаси  ·  {st.range_label} ({st.days_covered} дн.)",
           font=_font(30), fill=SUBTLE, anchor="ls")

    # Головні числа
    top_q = max(QUEUES, key=lambda q: st.per_queue[q])
    low_q = min(QUEUES, key=lambda q: st.per_queue[q])
    tiles = [
        (_hours(st.avg_minutes), "у середньому на чергу"),
        (_hours(st.per_queue[top_q]), f"найбільше — черга {top_q}"),
        (_hours(st.per_queue[low_q]), f"найменше — черга {low_q}"),
    ]
    gap = 16
    tile_w = (W - 2 * PAD - gap * (len(tiles) - 1)) / len(tiles)
    y = header_h
    for i, (value, label) in enumerate(tiles):
        x = PAD + i * (tile_w + gap)
        d.rounded_rectangle((x, y, x + tile_w, y + kpi_h), radius=14, fill=CARD)
        d.text((x + 24, y + 30), value, font=_font(48 if i == 0 else 40, True), fill=TEXT)
        d.text((x + 24, y + kpi_h - 28), label, font=_font(24), fill=MUTED, anchor="ls")

    # По чергах
    y = header_h + kpi_h + section_gap
    d.text((PAD, y), f"По чергах — годин без світла {st.period_word}", font=_font(26), fill=MUTED)
    y += 40
    x0 = PAD + 92
    x1 = W - PAD - 250
    scale = (x1 - x0) / max(1, max(st.per_queue.values()))
    for i, q in enumerate(QUEUES):
        if i and i % 2 == 0:
            y += GROUP_GAP
        cy = y + ROW_H / 2
        d.text((PAD + 18, cy), q, font=_font(28, True), fill=TEXT, anchor="lm")
        d.line((x0, y + 4, x0, y + ROW_H - 4), fill=HAIRLINE, width=1)  # базова лінія
        minutes = st.per_queue[q]
        xe = x0 + minutes * scale if minutes else x0
        if minutes:
            d.rounded_rectangle((x0, cy - BAR_H / 2, xe, cy + BAR_H / 2), radius=4, fill=OFF,
                                corners=(False, True, True, False))
        label = _hours(minutes)
        d.text((xe + 16, cy), label, font=_font(26, True), fill=TEXT, anchor="lm")
        lw = d.textlength(label, font=_font(26, True))
        d.text((xe + 16 + lw + 12, cy), _per_day(minutes, st.days_covered), font=_font(22), fill=MUTED, anchor="lm")
        y += ROW_H

    # По днях
    y += section_gap
    d.text((PAD, y), "По днях — у середньому на чергу, годин", font=_font(26), fill=MUTED)
    y += 40
    plot_top, plot_bottom = y + 24, y + daily_h - 40
    slot = (W - 2 * PAD - 40) / axis_days
    col_w = min(24, max(3, slot - 4))
    daily = {day: st.daily_avg(day) for day in st.per_day}
    peak = max(daily.values(), default=0) / 60
    top_tick = max(2, int(peak) + (1 if peak > int(peak) else 0))
    for tick in range(0, top_tick + 1, max(1, top_tick // 2)):
        yy = plot_bottom - (plot_bottom - plot_top) * tick / top_tick
        d.line((PAD + 40, yy, W - PAD, yy), fill=HAIRLINE, width=1)
        d.text((PAD + 30, yy), str(tick), font=_font(20), fill=MUTED, anchor="rm")
    peak_day = max(daily, key=daily.get) if daily else None
    label_every = 5 if st.is_month else max(7, axis_days // 8)
    for n in range(axis_days):
        day = st.axis_start + timedelta(days=n)
        cx = PAD + 40 + slot * (n + 0.5)
        if day in daily and daily[day] > 0:
            h = (plot_bottom - plot_top) * (daily[day] / 60) / top_tick
            d.rounded_rectangle((cx - col_w / 2, plot_bottom - h, cx + col_w / 2, plot_bottom), radius=4, fill=OFF,
                                corners=(True, True, False, False))
            if day == peak_day:
                d.text((cx, plot_bottom - h - 8), _hours(daily[day]).replace(" год", ""), font=_font(22, True),
                       fill=TEXT, anchor="mb")
        elif st.first_day <= day <= st.last_day:
            d.line((cx - col_w / 2, plot_bottom - 1, cx + col_w / 2, plot_bottom - 1), fill=GRID, width=2)
        last = n == axis_days - 1
        periodic = day.day % label_every == 0 if st.is_month else (n % label_every == 0 and axis_days - 1 - n >= 3)
        if n == 0 or last or periodic:  # у режимі «за весь час» дати довгі — не ставимо впритул до останньої
            text = str(day.day) if st.is_month else f"{day.day}.{day.month:02d}"
            d.text((cx, plot_bottom + 10), text, font=_font(20), fill=MUTED, anchor="mt")

    # Підпис
    d.text((PAD, H - 40), "Лише планові відключення за графіками обленерго (ГПВ); аварійні (ГАВ) не враховано.",
           font=_font(22), fill=MUTED, anchor="ls")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
