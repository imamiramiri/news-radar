"""Fetch news from public Telegram channel previews, RSS feeds and Google News RSS."""
from __future__ import annotations

import calendar
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup

from .models import Item
from .utils import clean, parse_iso, sha, truncate

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 20
MAX_TEXT = 1500


class FetchError(Exception):
    pass


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "fa,en;q=0.8"})
    return s


# ─────────────────────────── Telegram ───────────────────────────

def _parse_count(text: str | None) -> int | None:
    if not text:
        return None
    t = text.strip().upper().replace(",", "")
    m = re.match(r"([\d.]+)\s*([KM]?)", t)
    if not m:
        return None
    num = float(m.group(1))
    mult = {"K": 1_000, "M": 1_000_000}.get(m.group(2), 1)
    return int(num * mult)


def parse_telegram_html(html: str, ch: dict) -> list[Item]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[Item] = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        post = msg.get("data-post", "")
        text_el = msg.select_one(".tgme_widget_message_text")
        if text_el is None:
            continue
        for br in text_el.find_all("br"):
            br.replace_with("\n")
        text = clean(text_el.get_text(""))
        if len(text) < 15:
            continue
        if msg.select_one(".tgme_widget_message_video_player, video"):
            text = "[ویدیو] " + text
        elif msg.select_one(".tgme_widget_message_photo_wrap"):
            text = "[عکس] " + text
        t_el = msg.select_one("time[datetime]")
        published = parse_iso(t_el["datetime"]) if t_el else None
        views = _parse_count(getattr(msg.select_one(".tgme_widget_message_views"), "text", None))
        first_line = text.split("\n", 1)[0]
        items.append(Item(
            uid=f"tg:{post}",
            source=ch.get("name") or ch["username"],
            kind=ch.get("kind", "news"),
            lang=ch.get("lang", "fa"),
            title=truncate(first_line, 160),
            text=truncate(text, MAX_TEXT),
            url=f"https://t.me/{post}",
            published=published,
            weight=float(ch.get("weight", 1.0)),
            views=views,
            origin="telegram",
        ))
    return items


def fetch_telegram(session: requests.Session, ch: dict) -> list[Item]:
    url = f"https://t.me/s/{ch['username']}"
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    items = parse_telegram_html(r.text, ch)
    if not items:
        raise FetchError("هیچ پستی پیدا نشد (یوزرنیم اشتباه است یا پیش‌نمایش وب کانال بسته است)")
    return items


# ─────────────────────────── RSS ───────────────────────────

def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    return None


def _html_to_text(raw: str) -> str:
    if not raw:
        return ""
    return clean(BeautifulSoup(raw, "html.parser").get_text(" ", strip=True))


def parse_feed(content: bytes, src: dict, origin: str = "rss") -> list[Item]:
    feed = feedparser.parse(content)
    items: list[Item] = []
    for e in feed.entries:
        title = clean(e.get("title", ""))
        if not title:
            continue
        link = e.get("link", "")
        summary = _html_to_text(e.get("summary") or e.get("description") or "")
        source_name = src.get("name", "RSS")
        if origin == "google_news":
            publisher = (e.get("source") or {}).get("title") if isinstance(e.get("source"), dict) else None
            if publisher:
                source_name = publisher
                suffix = f" - {publisher}"
                if title.endswith(suffix):
                    title = title[: -len(suffix)].strip()
            summary = ""  # Google News summaries only repeat the title
        uid_basis = e.get("id") or link or title
        items.append(Item(
            uid=f"{origin}:{sha(uid_basis)}",
            source=source_name,
            kind=src.get("kind", "news"),
            lang=src.get("lang", "en"),
            title=truncate(title, 220),
            text=truncate(summary, MAX_TEXT),
            url=link,
            published=_entry_time(e),
            weight=float(src.get("weight", 1.0)),
            origin=origin,
        ))
    return items


def fetch_rss(session: requests.Session, src: dict) -> list[Item]:
    r = session.get(src["url"], timeout=TIMEOUT)
    r.raise_for_status()
    items = parse_feed(r.content, src, "rss")
    if not items:
        raise FetchError("فید خالی است یا قابل خواندن نیست")
    return items


def google_news_url(query: str, lang: str) -> str:
    if lang == "fa":
        params = "hl=fa&gl=IR&ceid=IR:fa"
    else:
        params = "hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&{params}"


def fetch_google_news(session: requests.Session, src: dict) -> list[Item]:
    r = session.get(google_news_url(src["query"], src.get("lang", "en")), timeout=TIMEOUT)
    r.raise_for_status()
    # an empty result is normal for narrow queries in quiet hours
    return parse_feed(r.content, src, "google_news")


# ─────────────────────────── all sources ───────────────────────────

def source_jobs(sources: dict) -> list[tuple[str, str, dict]]:
    jobs = []
    for ch in sources.get("telegram") or []:
        if ch.get("enabled", True):
            jobs.append(("telegram", ch.get("name") or ch["username"], ch))
    for src in sources.get("rss") or []:
        if src.get("enabled", True):
            jobs.append(("rss", src.get("name") or src["url"], src))
    for src in sources.get("google_news") or []:
        if src.get("enabled", True):
            jobs.append(("google_news", "گوگل‌نیوز · " + (src.get("name") or src["query"]), src))
    return jobs


FETCHERS = {"telegram": fetch_telegram, "rss": fetch_rss, "google_news": fetch_google_news}


def fetch_all(sources: dict, session: requests.Session | None = None, workers: int = 12):
    """Return (items, health). health maps source label -> (ok, count, error)."""
    session = session or make_session()
    jobs = source_jobs(sources)
    items: list[Item] = []
    health: dict[str, tuple[bool, int, str]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(FETCHERS[kind], session, cfg): (kind, label) for kind, label, cfg in jobs}
        for fut in as_completed(futures):
            kind, label = futures[fut]
            try:
                got = fut.result()
                items.extend(got)
                health[label] = (True, len(got), "")
            except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
                msg = f"{type(exc).__name__}: {exc}"[:200]
                health[label] = (False, 0, msg)
                log.warning("source failed: %s -> %s", label, msg)
    return items, health
