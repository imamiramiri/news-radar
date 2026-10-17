"""Group items from different sources that report the same story."""
from __future__ import annotations

import re

from .models import Cluster, Item
from .utils import AR_TO_FA, TO_EN_DIGITS

# Function words, units and generic "news-speak" that say nothing about *which* story it is.
STOPWORDS = set("""
و در به از که این را با برای تا یک آن هم شد شده است می ها های اما یا نیز خود بر هر کرد کرده
گفت گفته اعلام امروز دیروز روز شب خبر فوری عکس ویدیو ویدئو فیلم می‌شود میشه شود باید بود بودن خواهد
تومان تومن ریال هزار میلیون میلیارد درصد قیمت نرخ
گزارش ادعا برخی برخ منابع منبع درباره ماجرا چیست جزئیات فروند حکایت دارد داشت شدند کردند
کند کنند کنیم کنید کرده شوند بودند هست نیست داد دادند دهد دهند رسید رسیدند رسیده یافت یافته داده دارند
شنبه یکشنبه دوشنبه سه‌شنبه سه چهارشنبه پنجشنبه پنج‌شنبه جمعه
فروردین اردیبهشت خرداد تیر مرداد شهریور مهر ابان اذر دی بهمن اسفند
the a an of to in on for and or is are was were be been by with at from as that this it its
after over says said say will would could new news live update updates report reports amid
monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october november december
""".split())

YEAR_RE = re.compile(r"^(1[34]\d\d|19\d\d|20\d\d)$")


def _stem(word: str) -> str:
    """Very light suffix stripping so «اماراتی» matches «امارات» and «talks» matches «talk»."""
    if word.isascii():
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            return word[:-1]
        return word
    for suffix, min_len in (("های", 5), ("ها", 5), ("ی", 4)):
        if word.endswith(suffix) and len(word) >= min_len:
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> list[str]:
    t = (text or "").translate(AR_TO_FA).translate(TO_EN_DIGITS).lower()
    t = t.replace("\u200c", " ")
    t = re.sub(r"[^\w\s]", " ", t)
    out = []
    for w in t.split():
        if len(w) <= 1 or w in STOPWORDS or YEAR_RE.match(w):
            continue
        w = _stem(w)
        if len(w) > 1 and w not in STOPWORDS:
            out.append(w)
    return out


def text_tokens(title: str, text: str = "") -> set[str]:
    return set(tokens(f"{title} {(text or '')[:240]}")[:40])


def token_set(item: Item) -> set[str]:
    return text_tokens(item.title, item.text)


def similarity(a: set[str], b: set[str]) -> float:
    if len(a) < 3 or len(b) < 3:
        return 0.0
    overlap = len(a & b) / min(len(a), len(b))
    jaccard = len(a & b) / len(a | b)
    return max(jaccard, 0.85 * overlap)


def cluster_items(items: list[Item], threshold: float = 0.6) -> list[Cluster]:
    ordered = sorted(items, key=lambda i: (i.published is None, i.published))
    clusters: list[Cluster] = []
    for item in ordered:
        ts = token_set(item)
        best, best_sim = None, 0.0
        for c in clusters:
            s = similarity(ts, c.tokens)
            if s > best_sim:
                best, best_sim = c, s
        if best is not None and best_sim >= threshold:
            best.items.append(item)
        else:
            clusters.append(Cluster(items=[item], tokens=set(ts)))
    return clusters
