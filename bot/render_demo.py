"""python -m bot.render_demo — малює demo.png з останнього графіка у каналі."""
from datetime import date

import httpx

from .formatter import format_caption
from .parser import parse_post
from .render import render_schedule
from .scraper import parse_channel_html


def main() -> None:
    html = httpx.get("https://t.me/s/pat_cherkasyoblenergo", timeout=20).text
    for post in reversed(parse_channel_html(html)):
        schedule = parse_post(post.text, post.posted_at.date())
        if schedule:
            with open("demo.png", "wb") as f:
                f.write(render_schedule(schedule))
            print(format_caption(schedule, post.url))
            print("\n→ demo.png")
            return
    print("У останніх постах графіка не знайдено")


if __name__ == "__main__":
    main()
