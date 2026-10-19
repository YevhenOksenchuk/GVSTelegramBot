"""Оголошення обленерго без графіка: скасування ГПВ, аварійні відключення, «завтра без відключень».

Бот їх не публікує сам — надсилає власникам із кнопками «Опублікувати всім / Пропустити».
Тут лише розпізнавання й форматування; сам потік — у publisher.propose_announcement і handlers.
"""
from __future__ import annotations

import re
from html import escape

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
