"""Small helpers shared across modules."""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
TO_EN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
AR_TO_FA = str.maketrans({"ي": "ی", "ك": "ک", "ى": "ی", "ة": "ه", "ۀ": "ه", "أ": "ا", "إ": "ا", "آ": "ا"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_local(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(ZoneInfo(tz))


def fa(value) -> str:
    """Convert western digits to Persian digits."""
    return str(value).translate(FA_DIGITS)


def fa_time(dt: datetime, tz: str) -> str:
    return fa(to_local(dt, tz).strftime("%H:%M"))


def sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:16]


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def clean(text: str) -> str:
    text = (text or "").replace("\u200f", "").replace("\u200e", "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


def parse_hhmm(value: str) -> time:
    h, m = str(value).split(":")
    return time(int(h), int(m))


def in_daily_window(now_local: datetime, start: str, end: str) -> bool:
    """True if local time is inside [start, end); handles windows crossing midnight."""
    t = now_local.time()
    s, e = parse_hhmm(start), parse_hhmm(end)
    if s <= e:
        return s <= t < e
    return t >= s or t < e


def hours_ago(dt: datetime | None, now: datetime) -> float:
    if dt is None:
        return 0.0
    return (now - dt) / timedelta(hours=1)


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old in (None, 0):
        return None
    return (new - old) / old * 100.0


def fmt_num(value: float | None, decimals: int = 0) -> str:
    if value is None:
        return "—"
    if decimals == 0:
        return f"{value:,.0f}"
    return f"{value:,.{decimals}f}"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%"
