"""Оголошення обленерго без графіка: скасування ГПВ, аварійні відключення, «завтра без відключень».

Бот їх не публікує сам — надсилає власникам із кнопками «Опублікувати всім / Пропустити».
Тут лише розпізнавання й форматування; сам потік — у publisher.propose_announcement і handlers.
"""
from __future__ import annotations

import re
from html import escape
from typing import Optional, Tuple

# Явні ознаки важливого оголошення
STRONG = (
    r"скасов",                              # ГПВ скасовано
    r"відмін",                              # відмінено
    r"не\s+(?:буде|будуть)?\s*застосов",     # не застосовуватимуться
    r"без\s+відключень",
    r"відключень\s+не\s+(?:буде|планується)",
    r"\bгав\b",                             # графіки аварійних відключень
    r"графік[а-яіїє]*\s+не\s+діят",
)
# Слабкі ознаки — лише якщо йдеться про Черкащину (інакше це загальноукраїнські новини)
LOCAL_ONLY = (r"аварійн", r"екстрен", r"стабілізаційн", r"відновлен")
# Про що має бути пост, щоб узагалі розглядати його
TOPIC = (r"черкас", r"гпв", r"графік", r"відключен", r"електропостач", r"світл")
# Пости для бізнесу (ГОП) — не для людей
EXCLUDE = (r"для\s+промисловості\s+та\s+бізнесу", r"\bгоп\b")

MAX_LEN = 3500  # ліміт Telegram на текст — 4096 разом із заголовком і посиланням

# ГАВ — графіки аварійних відключень: публікуємо автоматично, коротко, якщо формулювання впізнаване
_GAV_WORD = r"(?:\bгав\b|графік[а-яіїє]*\s+аварійних\s+відключень)"
_GAV_START = re.compile(r"застосован[а-яіїє]*\s+графік[а-яіїє]*\s+аварійних\s+відключень", re.I)
_GAV_STOP = re.compile(
    r"(?:скасован|припинен|відмінен|завершен|знят)[а-яіїє]*[^.\n]{0,60}" + _GAV_WORD
    + r"|" + _GAV_WORD + r"[^.\n]{0,60}(?:скасован|припинен|відмінен|завершен|знят)",
    re.I,
)
_GAV_TIME = re.compile(r"\b[зо]\s+(\d{1,2}:\d{2})", re.I)


def classify_gav(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """("start" | "stop", "05:12" або None) для впізнаваного поста про ГАВ; інакше None (на модерацію)."""
    if not re.search(_GAV_WORD, text, re.I):
        return None
    when = _GAV_TIME.search(text)
    time = when.group(1) if when else None
    if _GAV_STOP.search(text):
        return "stop", time
    if _GAV_START.search(text):
        return "start", time
    return None


def format_gav(kind: str, time: Optional[str], url: str) -> str:
    if kind == "start":
        head = "🚨 <b>Аварійні відключення (ГАВ)" + (f" з {time}" if time else "") + "</b>"
    else:
        head = "✅ <b>Аварійні відключення (ГАВ) скасовано" + (f" з {time}" if time else "") + "</b>"
    return f"{head}\n<a href=\"{escape(url)}\">Черкасиобленерго</a>"


def is_announcement(text: str) -> bool:
    t = text.lower()
    if any(re.search(p, t) for p in EXCLUDE):
        return False
    if not any(re.search(p, t) for p in TOPIC):
        return False
    if any(re.search(p, t) for p in STRONG):
        return True
    return "черкас" in t and any(re.search(p, t) for p in LOCAL_ONLY)


def format_announcement(text: str, url: str) -> str:
    body = text.strip()
    if len(body) > MAX_LEN:
        body = body[: MAX_LEN - 1] + "…"
    return f"📢 <b>Черкасиобленерго</b>\n\n{escape(body)}\n\n<a href=\"{escape(url)}\">Джерело</a>"
