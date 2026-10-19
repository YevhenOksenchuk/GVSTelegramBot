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
CREATE TABLE IF NOT EXISTS posts (
    post_id      INTEGER PRIMARY KEY,
    text_hash    TEXT NOT NULL,
    is_schedule  INTEGER NOT NULL DEFAULT 0,
    text         TEXT,
    url          TEXT,
    posted_at    TEXT,
    seen_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


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
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    # --- чати ---
    async def add_chat(self, chat_id: int, title: str, type_: str, added_by: Optional[int]) -> None:
        await self.db.execute(
            """INSERT INTO chats (chat_id, title, type, added_by, active) VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, type=excluded.type, active=1""",
            (chat_id, title, type_, added_by),
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
