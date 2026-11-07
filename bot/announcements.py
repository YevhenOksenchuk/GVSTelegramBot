"""Оголошення обленерго без графіка: скасування ГПВ, аварійні відключення (ГАВ/СГАВ), «завтра без відключень».

ГАВ/СГАВ з упізнаваним формулюванням публікуються автоматично й коротко (publisher.publish_gav);
решта важливого йде власникам із кнопками «Опублікувати всім / Пропустити» (publisher.propose_announcement).
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
    r"\bс?гав\b",                           # графіки аварійних відключень (у т.ч. спеціальні)
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


# ---------------------------------------------------------------------------
# ГАВ — графіки аварійних відключень; СГАВ — спеціальні графіки аварійних відключень
# ---------------------------------------------------------------------------

_GAV_WORD = r"(?:\bс?гав\b|(?:спеціальн[а-яіїє]*\s+)?графік[а-яіїє]*\s+аварійних\s+відключень)"
_SPECIAL = re.compile(r"\bсгав\b|спеціальн[а-яіїє]*\s+графік[а-яіїє]*\s+аварійних", re.I)
# «застосовані (спеціальні) графіки аварійних відключень» — діють від зазначеного часу
_GAV_START = re.compile(
    r"застосов[а-яіїє]*\s+(?:спеціальн[а-яіїє]*\s+)?графік[а-яіїє]*\s+аварійних\s+відключень", re.I
)
_GAV_STOP = re.compile(
    r"(?:скасован|припинен|відмінен|завершен|знят)[а-яіїє]*[^.\n]{0,60}" + _GAV_WORD
    + r"|" + _GAV_WORD + r"[^.\n]{0,60}(?:скасован|припинен|відмінен|завершен|знят)",
    re.I,
)
_GAV_TIME = re.compile(r"\b[зо]\s+(\d{1,2}:\d{2})", re.I)
_GPV_NOTE = re.compile(r"[^.\n]*(?:продовжують\s+діяти|діють|діятимуть|не\s+діють)[^.\n]*\(ГПВ\)[^.\n]*\.", re.I)


def classify_gav(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """("start" | "special" | "stop", "05:12" або None) для впізнаваного поста про ГАВ; інакше None.

    Якщо в одному пості і «скасовано ГАВ», і «застосовано СГАВ» — головне те, що діє далі.
    """
    if not re.search(_GAV_WORD, text, re.I):
        return None
    when = _GAV_TIME.search(text)
    time = when.group(1) if when else None
    start = _GAV_START.search(text)
    if start:
        return ("special" if _SPECIAL.search(start.group(0)) else "start"), time
    if _GAV_STOP.search(text):
        return "stop", time
    return None


def is_special(text: str) -> bool:
    return bool(_SPECIAL.search(text))


def gav_note(text: str) -> Optional[str]:
    """Речення про ГПВ з поста обленерго (дослівно), напр. «Також продовжують діяти графіки … (ГПВ).»"""
    m = _GPV_NOTE.search(text)
    return m.group(0).strip() if m else None


def format_gav(kind: str, time: Optional[str], url: str, note: Optional[str] = None,
               special_stop: bool = False) -> str:
    when = f" з {time}" if time else ""
    if kind == "start":
        head = f"🚨 Аварійні відключення (ГАВ){when}"
    elif kind == "special":
        head = f"🚨 Спеціальні графіки аварійних відключень (СГАВ){when}"
    else:
        what = "Спеціальні графіки аварійних відключень (СГАВ)" if special_stop else "Аварійні відключення (ГАВ)"
        head = f"✅ {what} скасовано{when}"
    parts = [head]
    if note:
        parts.append(escape(note))
    parts.append(f'<a href="{escape(url)}">Черкасиобленерго</a>')
    return "\n\n".join(parts)
