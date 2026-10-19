"""Малювання графіка у PNG (Pillow)."""
from __future__ import annotations

import io
import os
from functools import lru_cache
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

from .parser import QUEUES, Schedule

W = 1080
PAD = 40
LABEL_W = 96
TOTAL_W = 120
ROW_H = 96
BAR_H = 36
ROW_GAP = 8
GROUP_GAP = 18
HEADER_H = 170
SCALE_H = 40
FOOTER_H = 90
LEGEND_ROW = 46

BG = "#11151C"
CARD = "#1A2029"
TEXT = "#F2F4F7"
MUTED = "#8B95A5"
SUBTLE = "#C9D1DC"
GRID = "#2A3240"
OFF = "#E5484D"
ON = "#2B3A50"
ON_EDGE = "#435A7A"
NOW = "#F5C451"
PAST = "#161B23"
OFF_PAST = "#5A2A30"
TIME_TEXT = "#B4BDC9"
ON_TEXT = "#9DB8DC"
GUIDE = "#1E2530"
GUIDE_MAJOR = "#2C3542"

_FONT_DIRS = [
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "fonts"),
    "/usr/share/fonts/truetype/dejavu",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
]
_REGULAR = ["Inter-Regular.ttf", "DejaVuSans.ttf", "Arial Unicode.ttf", "Arial.ttf"]
_BOLD = ["Inter-Bold.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf", "Arial Unicode.ttf"]


@lru_cache(maxsize=None)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for name in (_BOLD if bold else _REGULAR):
        for d in _FONT_DIRS:
            path = os.path.join(d, name)
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _fmt_hours(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if not m:
        return f"{h} год"
    if m == 30:
        return f"{h},5 год"
    return f"{h}г {m}хв"


def _hhmm(minute: int) -> str:
    h, m = divmod(minute, 60)
    return f"{h:02d}:{m:02d}"


def _rrect(d: ImageDraw.ImageDraw, box, radius: float, **kwargs) -> None:
    """rounded_rectangle, що не падає на дуже коротких відрізках (напр. 16:57–17:00)."""
    xa, ya, xb, yb = box
    if xb - xa < 2:
        mid = (xa + xb) / 2
        xa, xb = mid - 1, mid + 1
    radius = max(0, min(radius, (xb - xa) / 2, (yb - ya) / 2))
    d.rounded_rectangle((xa, ya, xb, yb), radius=radius, **kwargs)


def _row_positions() -> List[int]:
    ys, y = [], 0
    for i in range(len(QUEUES)):
        if i:
            y += GROUP_GAP if i % 2 == 0 else ROW_GAP
        ys.append(y)
        y += ROW_H
    return ys


def render_schedule(schedule: Schedule) -> bytes:
    """Повертає PNG з графіком."""
    rows = _row_positions()
    grid_h = rows[-1] + ROW_H
    top = HEADER_H + SCALE_H
    bottom = top + grid_h
    extra_legend = bool(schedule.changed or schedule.cutoff)
    H = bottom + SCALE_H + FOOTER_H + (LEGEND_ROW if extra_legend else 0)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Заголовок
    d.text((PAD, PAD), "Графік відключень світла", font=_font(50, True), fill=TEXT)
    # Усі частини на одній базовій лінії (anchor "ls"), бо жирний і звичайний шрифти мають різні відступи
    sx, baseline = PAD, PAD + 100
    if schedule.date_label:
        d.text((sx, baseline), schedule.date_label, font=_font(34, True), fill=TEXT, anchor="ls")
        sx += d.textlength(schedule.date_label, font=_font(34, True))
        d.text((sx, baseline), "  ·  ", font=_font(34), fill=SUBTLE, anchor="ls")
        sx += d.textlength("  ·  ", font=_font(34))
    d.text((sx, baseline), "м. Черкаси", font=_font(34), fill=SUBTLE, anchor="ls")
    if schedule.updated:
        badge = "ОНОВЛЕНО"
        if schedule.time_label:
            badge += f" о {schedule.time_label}"
        bf = _font(26, True)
        bw = d.textlength(badge, font=bf) + 36
        bx, by = W - PAD - bw, PAD + 66
        d.rounded_rectangle((bx, by, bx + bw, by + 44), radius=22, fill=NOW)
        d.text((bx + bw / 2, by + 22), badge, font=bf, fill=BG, anchor="mm")

    x0 = PAD + LABEL_W
    x1 = W - PAD - TOTAL_W
    hour_w = (x1 - x0) / 24

    def x_at(minute: float) -> float:
        return x0 + minute / 60 * hour_w

    # Шкала годин — кожна година, зверху й знизу
    def draw_scale(y_mid: float) -> None:
        for h in range(25):
            major = h % 3 == 0
            d.text((x_at(h * 60), y_mid), f"{h:02d}",
                   font=_font(20, major), fill=TEXT if major else MUTED, anchor="mm")

    draw_scale(top - SCALE_H / 2)
    draw_scale(bottom + SCALE_H / 2)

    # Картки рядків
    for q, ry in zip(QUEUES, rows):
        changed = q in schedule.changed
        d.rounded_rectangle((PAD, top + ry, W - PAD, top + ry + ROW_H), radius=12, fill=CARD,
                            outline=NOW if changed else None, width=2)

    # Вертикальна сітка через усю таблицю
    for h in range(25):
        xx = x_at(h * 60)
        d.line((xx, top - 6, xx, bottom + 6), fill=GUIDE_MAJOR if h % 3 == 0 else GUIDE, width=1)

    # Рядки
    cut = schedule.cutoff
    for q, ry in zip(QUEUES, rows):
        y = top + ry
        bar_top, bar_bot = y + 12, y + 12 + BAR_H
        d.text((PAD + 18, bar_top + BAR_H / 2), q, font=_font(30, True), fill=TEXT, anchor="lm")
        if q in schedule.changed:
            d.text((W - PAD - 18, bar_bot + 10), "змінено", font=_font(20, True), fill=NOW, anchor="ra")

        d.rounded_rectangle((x0, bar_top, x1, bar_bot), radius=8, fill=ON, outline=ON_EDGE)
        if cut:  # минула частина дня — притемнена
            _rrect(d, (x0, bar_top, x_at(cut), bar_bot), 8, fill=PAST)
            d.rectangle((max(x0, x_at(cut) - 8), bar_top, x_at(cut), bar_bot), fill=PAST)
        for h in range(1, 24):
            xx = x_at(h * 60)
            d.line((xx, bar_top + 4, xx, bar_bot - 4), fill=GRID if h % 3 else ON_EDGE, width=1)

        def block(s_: int, e_: int, color: str) -> None:
            _rrect(d, (x_at(s_) + 1, bar_top, x_at(e_) - 1, bar_bot), 8, fill=color)

        intervals = schedule.queues.get(q, [])
        for s_, e_ in schedule.past_from_previous(q):
            block(s_, e_, OFF_PAST)
        for s_, e_ in intervals:
            if e_ <= cut:
                block(s_, e_, OFF_PAST)
            elif s_ < cut:  # триває на момент оновлення: минула частина бліда, решта яскрава
                block(s_, e_, OFF_PAST)
                block(cut, e_, OFF)
                d.rectangle((x_at(cut), bar_top, max(x_at(cut), min(x_at(cut) + 8, x_at(e_) - 1)), bar_bot),
                            fill=OFF)
            else:
                block(s_, e_, OFF)
        added, removed = schedule.added(q), schedule.removed(q)
        for s_, e_ in added:
            _rrect(d, (x_at(s_), bar_top - 3, x_at(e_), bar_bot + 3), 9, outline=NOW, width=4)
        for s_, e_ in removed:
            _rrect(d, (x_at(s_) + 2, bar_top + 2, x_at(e_) - 2, bar_bot - 2), 7,
                   outline=NOW, width=3)

        # Точний час під смугою: змінені інтервали — жовтим, скасовані — закресленими
        parts = []  # (текст, колір, закреслено)
        for s_, e_ in intervals:
            is_new = any(a_ < e_ and s_ < b_ for a_, b_ in added)
            parts.append((f"{_hhmm(s_)}–{_hhmm(e_)}", NOW if is_new else TIME_TEXT, False))
        if not intervals:
            parts.append(("без відключень", ON_TEXT, False))
        for s_, e_ in removed:
            parts.append((f"{_hhmm(s_)}–{_hhmm(e_)}", NOW, True))
        sep = "   "
        size = 24
        while size > 15 and d.textlength(sep.join(t for t, _, _ in parts), font=_font(size)) > x1 - x0:
            size -= 1
        f = _font(size)
        tx, ty = x0, bar_bot + 10
        for i, (t, color, strike) in enumerate(parts):
            if i:
                tx += d.textlength(sep, font=f)
            tw = d.textlength(t, font=f)
            d.text((tx, ty), t, font=f, fill=color)
            if strike:
                d.line((tx, ty + size * 0.62, tx + tw, ty + size * 0.62), fill=color, width=2)
            tx += tw

        total = schedule.off_minutes(q)
        d.text((W - PAD - 18, bar_top + BAR_H / 2), _fmt_hours(total) if total else "—",
               font=_font(26, True), fill=OFF if total else MUTED, anchor="rm")

    # Легенда
    ly = bottom + SCALE_H + 30
    d.rounded_rectangle((PAD, ly, PAD + 36, ly + 26), radius=6, fill=OFF)
    d.text((PAD + 48, ly + 13), "немає світла", font=_font(26), fill=TEXT, anchor="lm")
    lx = PAD + 300
    d.rounded_rectangle((lx, ly, lx + 36, ly + 26), radius=6, fill=ON, outline=ON_EDGE)
    d.text((lx + 48, ly + 13), "світло є", font=_font(26), fill=TEXT, anchor="lm")
    d.text((W - PAD - 18, ly + 13), "год — сумарно без світла", font=_font(24), fill=MUTED, anchor="rm")
    if extra_legend:
        ly += LEGEND_ROW
        lx = PAD
        if schedule.changed:
            d.rounded_rectangle((lx, ly, lx + 36, ly + 26), radius=6, fill=OFF, outline=NOW, width=3)
            d.text((lx + 48, ly + 13), "нові години", font=_font(26), fill=TEXT, anchor="lm")
            lx += 300
            d.rounded_rectangle((lx, ly, lx + 36, ly + 26), radius=6, fill=ON, outline=NOW, width=3)
            d.text((lx + 48, ly + 13), "скасовано", font=_font(26), fill=TEXT, anchor="lm")
            lx += 260
        if cut:
            d.rounded_rectangle((lx, ly, lx + 36, ly + 26), radius=6, fill=PAST, outline=ON_EDGE)
            d.text((lx + 48, ly + 13), f"минуло (до {_hhmm(cut)})", font=_font(26), fill=TEXT, anchor="lm")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
