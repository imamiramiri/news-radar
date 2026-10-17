"""Group items from different sources that report the same story."""
from __future__ import annotations

import re

from .models import Cluster, Item
from .utils import AR_TO_FA, TO_EN_DIGITS

STOPWORDS = set("""
و در به از که این را با برای تا یک آن هم شد شده است می ها های اما یا نیز خود بر هر کرد کرده
گفت گفته اعلام امروز دیروز روز شب خبر فوری عکس ویدیو می‌شود میشه شود باید بود بودن خواهد
تومان تومن ریال هزار میلیون میلیارد درصد قیمت
the a an of to in on for and or is are was were be been by with at from as that this it its
after over says said say will would could new news live update updates report reports amid
""".split())


def tokens(text: str) -> list[str]:
    t = (text or "").translate(AR_TO_FA).translate(TO_EN_DIGITS).lower()
    t = t.replace("\u200c", " ")
    t = re.sub(r"[^\w\s]", " ", t)
    return [w for w in t.split() if len(w) > 1 and w not in STOPWORDS]


def token_set(item: Item) -> set[str]:
    return set(tokens(f"{item.title} {item.text[:240]}")[:40])


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
