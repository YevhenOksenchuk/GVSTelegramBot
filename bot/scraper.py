"""Читання публічної веб-версії каналу https://t.me/s/<channel>."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import List

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (compatible; GVSScheduleBot/1.0)"


@dataclass
class Post:
    post_id: int
    text: str
    posted_at: datetime
    url: str


def parse_channel_html(html: str) -> List[Post]:
    soup = BeautifulSoup(html, "html.parser")
    posts: List[Post] = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        ref = msg["data-post"]  # "channel/1806"
        m = re.search(r"/(\d+)$", ref)
        text_el = msg.select_one("div.tgme_widget_message_text")
        time_el = msg.select_one("time[datetime]")
        if not m or not text_el or not time_el:
            continue
        for br in text_el.find_all("br"):
            br.replace_with("\n")
        posts.append(Post(
            post_id=int(m.group(1)),
            text=text_el.get_text().strip(),
            posted_at=datetime.fromisoformat(time_el["datetime"]),
            url=f"https://t.me/{ref}",
        ))
    return sorted(posts, key=lambda p: p.post_id)


async def fetch_posts(client: httpx.AsyncClient, channel: str) -> List[Post]:
    resp = await client.get(
        f"https://t.me/s/{channel}",
        headers={"User-Agent": USER_AGENT},
        timeout=20,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return parse_channel_html(resp.text)
