"""Data classes for news items and clusters of the same story."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

KIND_PRIORITY = {"official": 5, "news": 4, "econ": 4, "macro": 4, "crypto": 3, "competitor": 1}


@dataclass
class Item:
    uid: str
    source: str
    kind: str
    lang: str
    title: str
    text: str
    url: str
    published: datetime | None
    weight: float = 1.0
    views: int | None = None
    origin: str = ""  # telegram | rss | google_news

    def rank_key(self):
        return (KIND_PRIORITY.get(self.kind, 2), self.weight, len(self.text))


@dataclass
class Cluster:
    items: list[Item]
    tokens: set[str] = field(default_factory=set)
    # filled after triage
    score: int = 0
    category: str = ""
    assets: list[str] = field(default_factory=list)
    novelty: str = "new"
    credibility: str = "medium"
    reason: str = ""

    @property
    def rep(self) -> Item:
        return max(self.items, key=lambda i: i.rank_key())

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for it in self.items:
            if it.source not in seen:
                seen.append(it.source)
        return seen

    @property
    def independent_sources(self) -> int:
        return len({it.source for it in self.items if it.kind != "competitor"})

    @property
    def uids(self) -> list[str]:
        return [i.uid for i in self.items]

    @property
    def earliest(self) -> datetime | None:
        times = [i.published for i in self.items if i.published]
        return min(times) if times else None

    @property
    def only_competitors(self) -> bool:
        return all(i.kind == "competitor" for i in self.items)
