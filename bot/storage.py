"""SQLite: чати-підписники та вже оброблені пости."""
from __future__ import annotations

import hashlib
import os
from typing import List, Optional, Tuple

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id   INTEGER PRIMARY KEY,
    title     TEXT,
    type      TEXT,
    added_by  INTEGER,
    active    INTEGER NOT NULL DEFAULT 1,
    added_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    name        TEXT,
    username    TEXT,
    status      TEXT NOT NULL,  -- allowed | pending | denied
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS access_requests (
    user_id     INTEGER NOT NULL,  -- хто просить доступ
    chat_id     INTEGER NOT NULL,  -- кому з власників надіслано запит
    message_id  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,  -- службові позначки: last_backup_day тощо
    value  TEXT
);
CREATE TABLE IF NOT EXISTS posts (
    post_id      INTEGER PRIMARY KEY,
    text_hash    TEXT NOT NULL,
    is_schedule  INTEGER NOT NULL DEFAULT 0,
    text         TEXT,
    url          TEXT,
    posted_at    TEXT,
    seen_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sent_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    post_id     INTEGER,            -- пост обленерго, з якого зроблено повідомлення
    day         TEXT,               -- дата графіка YYYY-MM-DD; NULL для оголошень
    kind        TEXT NOT NULL,      -- photo | text
    sent_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS sent_messages_day ON sent_messages(day, chat_id);
CREATE TABLE IF NOT EXISTS owner_prompts (
    kind        TEXT NOT NULL,      -- announcement
    ref_id      INTEGER NOT NULL,   -- post_id
    chat_id     INTEGER NOT NULL,   -- власник, якому надіслано запит
    message_id  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS announcements (
    post_id     INTEGER PRIMARY KEY,
    status      TEXT NOT NULL,      -- pending | published | skipped
    decided_by  INTEGER,
    decided_at  TEXT
);
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id       INTEGER PRIMARY KEY,
    queue         TEXT,                         -- підчерга, напр. 3.2
    lead_minutes  INTEGER NOT NULL DEFAULT 30,  -- за скільки хвилин нагадувати про відключення
    notify_back   INTEGER NOT NULL DEFAULT 1,   -- повідомляти, коли світло має з'явитися
    updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reminders_sent (
    user_id   INTEGER NOT NULL,
    kind      TEXT NOT NULL,      -- pre | back | cancel
    event_at  TEXT NOT NULL,      -- момент події (ISO, Київ) — захист від повторів
    sent_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, kind, event_at)
);
CREATE TABLE IF NOT EXISTS chat_reminders_sent (
    chat_id   INTEGER NOT NULL,   -- канал/група
    queue     TEXT NOT NULL,      -- підчерга, про яку вже попередили
    event_at  TEXT NOT NULL,      -- початок відключення (ISO, Київ)
    sent_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (chat_id, queue, event_at)
);
"""

# Колонки, додані пізніше за створення таблиці (для баз, що вже існують)
_CHAT_COLUMNS = (
    ("pinned_message_id", "INTEGER"),               # останнє закріплене ботом повідомлення
    ("pin_enabled", "INTEGER NOT NULL DEFAULT 1"),  # чи закріплювати графіки в цьому чаті
    ("pin_failed_at", "TEXT"),                      # коли востаннє не вдалося (немає прав) — не повторюємо 24 год
    ("username", "TEXT"),                           # публічний @username чату — для посилань на графік
    ("remind_lead", "INTEGER NOT NULL DEFAULT 0"),  # нагадування в чат за N хв до відключення; 0 = вимкнено
)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.db: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        await self.db.executescript(SCHEMA)
        await self._migrate()
        await self.db.commit()

    async def _migrate(self) -> None:
        async with self.db.execute("PRAGMA table_info(chats)") as cur:
            existing = {row[1] for row in await cur.fetchall()}
        for name, ddl in _CHAT_COLUMNS:
            if name not in existing:
                await self.db.execute(f"ALTER TABLE chats ADD COLUMN {name} {ddl}")

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    # --- чати ---
    async def add_chat(self, chat_id: int, title: str, type_: str, added_by: Optional[int],
                       username: Optional[str] = None) -> None:
        await self.db.execute(
            """INSERT INTO chats (chat_id, title, type, added_by, active, username) VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, type=excluded.type, active=1,
                   username=COALESCE(excluded.username, chats.username)""",
            (chat_id, title, type_, added_by, username),
        )
        await self.db.commit()

    async def deactivate_chat(self, chat_id: int) -> None:
        await self.db.execute("UPDATE chats SET active=0 WHERE chat_id=?", (chat_id,))
        await self.db.commit()

    async def migrate_chat(self, old_id: int, new_id: int) -> None:
        await self.db.execute("UPDATE OR REPLACE chats SET chat_id=? WHERE chat_id=?", (new_id, old_id))
        await self.db.commit()

    async def is_active(self, chat_id: int) -> bool:
        async with self.db.execute("SELECT active FROM chats WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
        return bool(row and row[0])

    async def active_chats(self) -> List[int]:
        async with self.db.execute("SELECT chat_id FROM chats WHERE active=1") as cur:
            return [r[0] for r in await cur.fetchall()]

    async def active_chats_typed(self) -> List[Tuple[int, str]]:
        """(chat_id, type) усіх активних чатів."""
        async with self.db.execute("SELECT chat_id, type FROM chats WHERE active=1 ORDER BY chat_id") as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]

    # --- закріплення ---
    async def pin_state(self, chat_id: int) -> Tuple[Optional[int], bool, Optional[str]]:
        """(pinned_message_id, pin_enabled, pin_failed_at)"""
        async with self.db.execute(
            "SELECT pinned_message_id, pin_enabled, pin_failed_at FROM chats WHERE chat_id=?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
        return (row[0], bool(row[1]), row[2]) if row else (None, True, None)

    async def set_pinned(self, chat_id: int, message_id: int) -> None:
        await self.db.execute(
            "UPDATE chats SET pinned_message_id=?, pin_failed_at=NULL WHERE chat_id=?", (message_id, chat_id)
        )
        await self.db.commit()

    async def set_pin_enabled(self, chat_id: int, enabled: bool) -> None:
        await self.db.execute(
            "UPDATE chats SET pin_enabled=?, pin_failed_at=NULL WHERE chat_id=?", (int(enabled), chat_id)
        )
        await self.db.commit()

    async def set_pin_failed(self, chat_id: int, when: Optional[str]) -> None:
        await self.db.execute("UPDATE chats SET pin_failed_at=? WHERE chat_id=?", (when, chat_id))
        await self.db.commit()

    async def connected_chats_pins(self) -> List[Tuple[int, str, str, bool]]:
        """(chat_id, title, type, pin_enabled) активних каналів і груп."""
        async with self.db.execute(
            "SELECT chat_id, title, type, pin_enabled FROM chats WHERE active=1 AND type != 'private' "
            "ORDER BY type, title"
        ) as cur:
            return [(r[0], r[1], r[2], bool(r[3])) for r in await cur.fetchall()]

    # --- надіслані повідомлення (для закріплення, /fix) ---
    async def record_sent(self, chat_id: int, message_id: int, post_id: Optional[int],
                          day: Optional[str], kind: str) -> None:
        await self.db.execute(
            "INSERT INTO sent_messages (chat_id, message_id, post_id, day, kind) VALUES (?, ?, ?, ?, ?)",
            (chat_id, message_id, post_id, day, kind),
        )
        await self.db.commit()

    async def latest_sent_per_chat(self, day: str) -> List[Tuple[int, int, str]]:
        """Для кожного активного чату — найсвіжіше повідомлення бота з графіком на цю дату."""
        async with self.db.execute(
            "SELECT chat_id, message_id, kind FROM sent_messages WHERE id IN "
            "(SELECT MAX(id) FROM sent_messages WHERE day=? GROUP BY chat_id) "
            "AND chat_id IN (SELECT chat_id FROM chats WHERE active=1) ORDER BY chat_id",
            (day,),
        ) as cur:
            return [(r[0], r[1], r[2]) for r in await cur.fetchall()]

    async def sent_for_post(self, post_id: int) -> List[Tuple[int, int, str]]:
        async with self.db.execute(
            "SELECT chat_id, message_id, kind FROM sent_messages WHERE post_id=? ORDER BY id", (post_id,)
        ) as cur:
            return [(r[0], r[1], r[2]) for r in await cur.fetchall()]

    async def sent_days(self, limit: int = 3) -> List[Tuple[str, int]]:
        """(день, кількість чатів) за датами графіків, від найсвіжішої."""
        async with self.db.execute(
            "SELECT day, COUNT(DISTINCT chat_id) FROM sent_messages WHERE day IS NOT NULL "
            "GROUP BY day ORDER BY day DESC LIMIT ?",
            (limit,),
        ) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]

    # --- запити власникам (оголошення) ---
    async def save_prompt(self, kind: str, ref_id: int, chat_id: int, message_id: int) -> None:
        await self.db.execute(
            "INSERT INTO owner_prompts (kind, ref_id, chat_id, message_id) VALUES (?, ?, ?, ?)",
            (kind, ref_id, chat_id, message_id),
        )
        await self.db.commit()

    async def pop_prompts(self, kind: str, ref_id: int) -> List[Tuple[int, int]]:
        async with self.db.execute(
            "SELECT chat_id, message_id FROM owner_prompts WHERE kind=? AND ref_id=?", (kind, ref_id)
        ) as cur:
            rows = [(r[0], r[1]) for r in await cur.fetchall()]
        await self.db.execute("DELETE FROM owner_prompts WHERE kind=? AND ref_id=?", (kind, ref_id))
        await self.db.commit()
        return rows

    async def announcement_status(self, post_id: int) -> Optional[str]:
        async with self.db.execute("SELECT status FROM announcements WHERE post_id=?", (post_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_announcement(self, post_id: int, status: str, decided_by: Optional[int] = None) -> None:
        await self.db.execute(
            """INSERT INTO announcements (post_id, status, decided_by, decided_at)
               VALUES (?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END)
               ON CONFLICT(post_id) DO UPDATE SET status=excluded.status, decided_by=excluded.decided_by,
                   decided_at=excluded.decided_at""",
            (post_id, status, decided_by, decided_by),
        )
        await self.db.commit()

    async def connected_chats(self) -> List[Tuple[int, str, str]]:
        """(chat_id, title, type) активних каналів і груп (без особистих)."""
        async with self.db.execute(
            "SELECT chat_id, title, type FROM chats WHERE active=1 AND type != 'private' "
            "ORDER BY type, title"
        ) as cur:
            return list(await cur.fetchall())

    async def get_chat_title(self, chat_id: int) -> Optional[str]:
        async with self.db.execute("SELECT title FROM chats WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def get_chat_type(self, chat_id: int) -> Optional[str]:
        async with self.db.execute("SELECT type FROM chats WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    # --- службові позначки ---
    async def get_meta(self, key: str) -> Optional[str]:
        async with self.db.execute("SELECT value FROM meta WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self.db.commit()

    async def stats(self) -> List[Tuple[str, int]]:
        async with self.db.execute(
            "SELECT type, COUNT(*) FROM chats WHERE active=1 GROUP BY type"
        ) as cur:
            return list(await cur.fetchall())

    async def chats_added_by(self, user_id: int) -> List[int]:
        async with self.db.execute(
            "SELECT chat_id FROM chats WHERE active=1 AND (added_by=? OR chat_id=?)", (user_id, user_id)
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

    # --- доступ ---
    async def user_status(self, user_id: int) -> Optional[str]:
        async with self.db.execute("SELECT status FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_user(self, user_id: int, status: str, name: Optional[str] = None,
                       username: Optional[str] = None) -> None:
        await self.db.execute(
            """INSERT INTO users (user_id, name, username, status) VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET status=excluded.status,
                   name=COALESCE(excluded.name, users.name),
                   username=COALESCE(excluded.username, users.username),
                   updated_at=CURRENT_TIMESTAMP""",
            (user_id, name, username, status),
        )
        await self.db.commit()

    async def get_user(self, user_id: int) -> Optional[Tuple[str, Optional[str], str]]:
        """(name, username, status)"""
        async with self.db.execute(
            "SELECT name, username, status FROM users WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return tuple(row) if row else None

    async def list_users(self) -> List[Tuple[int, str, Optional[str], str]]:
        """(user_id, name, username, status), спершу ті, що чекають."""
        async with self.db.execute(
            "SELECT user_id, name, username, status FROM users "
            "ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'allowed' THEN 1 ELSE 2 END, name"
        ) as cur:
            return list(await cur.fetchall())

    async def save_request_message(self, user_id: int, chat_id: int, message_id: int) -> None:
        await self.db.execute(
            "INSERT INTO access_requests (user_id, chat_id, message_id) VALUES (?, ?, ?)",
            (user_id, chat_id, message_id),
        )
        await self.db.commit()

    async def pop_request_messages(self, user_id: int) -> List[Tuple[int, int]]:
        """(chat_id, message_id) запитів цього користувача, надісланих власникам; видаляє їх."""
        async with self.db.execute(
            "SELECT chat_id, message_id FROM access_requests WHERE user_id=?", (user_id,)
        ) as cur:
            rows = list(await cur.fetchall())
        await self.db.execute("DELETE FROM access_requests WHERE user_id=?", (user_id,))
        await self.db.commit()
        return rows

    # --- моя черга / нагадування ---
    async def get_prefs(self, user_id: int) -> Optional[Tuple[Optional[str], int, bool]]:
        """(queue, lead_minutes, notify_back) або None, якщо користувач нічого не налаштовував."""
        async with self.db.execute(
            "SELECT queue, lead_minutes, notify_back FROM user_prefs WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return (row[0], row[1], bool(row[2])) if row else None

    async def all_prefs(self) -> List[Tuple[int, str, int, bool]]:
        """(user_id, queue, lead_minutes, notify_back) усіх, хто обрав чергу."""
        async with self.db.execute(
            "SELECT user_id, queue, lead_minutes, notify_back FROM user_prefs WHERE queue IS NOT NULL"
        ) as cur:
            return [(r[0], r[1], r[2], bool(r[3])) for r in await cur.fetchall()]

    async def set_pref(self, user_id: int, field: str, value) -> None:
        assert field in ("queue", "lead_minutes", "notify_back")
        if isinstance(value, bool):
            value = int(value)
        await self.db.execute(
            f"INSERT INTO user_prefs (user_id, {field}) VALUES (?, ?) "
            f"ON CONFLICT(user_id) DO UPDATE SET {field}=excluded.{field}, updated_at=CURRENT_TIMESTAMP",
            (user_id, value),
        )
        await self.db.commit()

    async def clear_prefs(self, user_id: int) -> None:
        await self.db.execute("DELETE FROM user_prefs WHERE user_id=?", (user_id,))
        await self.db.commit()

    async def reminder_sent(self, user_id: int, kind: str, event_at: str) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM reminders_sent WHERE user_id=? AND kind=? AND event_at=?", (user_id, kind, event_at)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_reminder(self, user_id: int, kind: str, event_at: str) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO reminders_sent (user_id, kind, event_at) VALUES (?, ?, ?)",
            (user_id, kind, event_at),
        )
        await self.db.commit()

    async def sent_reminder_times(self, user_id: int, kind: str) -> List[str]:
        async with self.db.execute(
            "SELECT event_at FROM reminders_sent WHERE user_id=? AND kind=?", (user_id, kind)
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

    async def prune_reminders(self, days: int = 3) -> None:
        for table in ("reminders_sent", "chat_reminders_sent"):
            await self.db.execute(f"DELETE FROM {table} WHERE sent_at < datetime('now', ?)", (f"-{days} days",))
        await self.db.commit()

    # --- нагадування в каналах і групах ---
    async def set_chat_username(self, chat_id: int, username: Optional[str]) -> None:
        await self.db.execute("UPDATE chats SET username=? WHERE chat_id=?", (username, chat_id))
        await self.db.commit()

    async def set_remind_lead(self, chat_id: int, lead: int) -> None:
        await self.db.execute("UPDATE chats SET remind_lead=? WHERE chat_id=?", (lead, chat_id))
        await self.db.commit()

    async def connected_chats_reminders(self) -> List[Tuple[int, str, str, int]]:
        """(chat_id, title, type, remind_lead) активних каналів і груп."""
        async with self.db.execute(
            "SELECT chat_id, title, type, remind_lead FROM chats WHERE active=1 AND type != 'private' "
            "ORDER BY type, title"
        ) as cur:
            return [(r[0], r[1], r[2], r[3]) for r in await cur.fetchall()]

    async def chats_with_reminders(self) -> List[Tuple[int, str, Optional[str], int]]:
        """(chat_id, type, username, remind_lead) чатів, де нагадування увімкнено."""
        async with self.db.execute(
            "SELECT chat_id, type, username, remind_lead FROM chats WHERE active=1 AND remind_lead > 0"
        ) as cur:
            return [(r[0], r[1], r[2], r[3]) for r in await cur.fetchall()]

    async def chat_reminder_sent(self, chat_id: int, queue: str, event_at: str) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM chat_reminders_sent WHERE chat_id=? AND queue=? AND event_at=?", (chat_id, queue, event_at)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_chat_reminder(self, chat_id: int, queues: List[str], event_at: str) -> None:
        await self.db.executemany(
            "INSERT OR IGNORE INTO chat_reminders_sent (chat_id, queue, event_at) VALUES (?, ?, ?)",
            [(chat_id, q, event_at) for q in queues],
        )
        await self.db.commit()

    async def latest_sent_message(self, chat_id: int, day: Optional[str] = None) -> Optional[int]:
        """message_id останнього графіка бота в чаті (за день, якщо є; інакше будь-який)."""
        for condition, params in ((" AND day=?", (chat_id, day)), ("", (chat_id,))):
            if condition and not day:
                continue
            async with self.db.execute(
                f"SELECT message_id FROM sent_messages WHERE chat_id=? AND day IS NOT NULL{condition} "
                "ORDER BY id DESC LIMIT 1", params,
            ) as cur:
                row = await cur.fetchone()
            if row:
                return row[0]
        return None

    # --- пости ---
    async def has_any_posts(self) -> bool:
        async with self.db.execute("SELECT 1 FROM posts LIMIT 1") as cur:
            return await cur.fetchone() is not None

    async def get_post_hash(self, post_id: int) -> Optional[str]:
        async with self.db.execute("SELECT text_hash FROM posts WHERE post_id=?", (post_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def save_post(self, post_id: int, text: str, url: str, posted_at: str, is_schedule: bool) -> None:
        await self.db.execute(
            """INSERT INTO posts (post_id, text_hash, is_schedule, text, url, posted_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(post_id) DO UPDATE SET text_hash=excluded.text_hash,
                   is_schedule=excluded.is_schedule, text=excluded.text""",
            (post_id, text_hash(text), int(is_schedule), text, url, posted_at),
        )
        await self.db.commit()

    async def get_post_text(self, post_id: int) -> Optional[str]:
        async with self.db.execute("SELECT text FROM posts WHERE post_id=?", (post_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def get_post(self, post_id: int) -> Optional[Tuple[str, str, str]]:
        """(text, url, posted_at)"""
        async with self.db.execute("SELECT text, url, posted_at FROM posts WHERE post_id=?", (post_id,)) as cur:
            row = await cur.fetchone()
        return tuple(row) if row else None

    async def schedule_posts_before(self, post_id: int, limit: int = 30) -> List[Tuple[str, str]]:
        """(text, posted_at) попередніх постів з графіком, від новіших до старіших."""
        async with self.db.execute(
            "SELECT text, posted_at FROM posts WHERE is_schedule=1 AND post_id<? "
            "ORDER BY post_id DESC LIMIT ?",
            (post_id, limit),
        ) as cur:
            return list(await cur.fetchall())

    async def schedule_posts_after(self, post_id: int) -> List[Tuple[str, str]]:
        """(text, posted_at) новіших постів з графіком."""
        async with self.db.execute(
            "SELECT text, posted_at FROM posts WHERE is_schedule=1 AND post_id>? ORDER BY post_id",
            (post_id,),
        ) as cur:
            return list(await cur.fetchall())

    async def recent_schedule_posts(self, limit: int = 10) -> List[Tuple[int, str, str, str]]:
        """(post_id, text, url, posted_at) останніх постів з графіком, від новіших до старіших."""
        async with self.db.execute(
            "SELECT post_id, text, url, posted_at FROM posts WHERE is_schedule=1 "
            "ORDER BY post_id DESC LIMIT ?",
            (limit,),
        ) as cur:
            return list(await cur.fetchall())
