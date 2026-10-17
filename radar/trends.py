"""Cross-run story memory and «ترند» detection.

Each run only sees *new* items, but a story usually trickles in over several runs
(one outlet at 18:15, three more around 20:00...). So we keep a short memory of
recent stories — which sources covered each one and when — and flag a story as
trending as soon as at least N different sources covered it within a time window.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .cluster import similarity, text_tokens
from .models import Cluster
from .utils import iso, parse_iso, sha, truncate

MAX_TITLES = 8

# Used when settings.yaml has no `trend:` block, so the rule is safe out of the box.
DEFAULT_ROUTINE = ["قیمت .*امروز", "نرخ .*امروز", "پیش.?بینی قیمت", "بورس امروز", "سهام عدالت",
                   "اوقات شرعی", "^فال "]
DEFAULT_IGNORE = ["شهرخبر"]


def trend_window(source_times: dict[str, datetime], window: timedelta):
    """Largest number of distinct sources whose first report falls inside one window.

    Returns (count, start, end)."""
    times = sorted((t, s) for s, t in source_times.items())
    best = (0, None, None)
    j = 0
    for i in range(len(times)):
        while times[i][0] - times[j][0] > window:
            j += 1
        count = i - j + 1
        if count > best[0]:
            best = (count, times[j][0], times[i][0])
    return best


class TrendTracker:
    def __init__(self, state, cfg: dict, now: datetime):
        self.cfg = cfg or {}
        self.now = now
        self.window = timedelta(minutes=int(self.cfg.get("window_minutes", 60)))
        self.stale = timedelta(minutes=int(self.cfg.get("stale_minutes", 150)))
        self.min_sources = int(self.cfg.get("min_sources", 3))
        self.threshold = float(self.cfg.get("similarity", 0.5))
        self.min_shared = int(self.cfg.get("min_shared_words", 3))
        self.aliases = {k: v for k, v in (self.cfg.get("aliases") or {}).items()}
        self.ignore = set(self.cfg.get("ignore_sources", DEFAULT_IGNORE) or [])
        self.routine = [re.compile(p) for p in (self.cfg.get("routine_patterns", DEFAULT_ROUTINE) or [])]
        memory = timedelta(hours=float(self.cfg.get("memory_hours", 8)))
        self.stories: list[dict] = state.data.setdefault("stories", [])
        self.stories[:] = [s for s in self.stories
                           if (parse_iso(s.get("last")) or now) >= now - memory]
        self._tok_cache: dict[str, list[set[str]]] = {}

    # ── helpers ──
    def source_name(self, source: str) -> str | None:
        name = self.aliases.get(source, source)
        return None if name in self.ignore else name

    def is_routine(self, title: str) -> bool:
        return any(p.search(title or "") for p in self.routine)

    def _story_tokens(self, story: dict) -> list[set[str]]:
        key = story["id"] + str(len(story["titles"]))
        if key not in self._tok_cache:
            self._tok_cache[key] = [text_tokens(t["t"]) for t in story["titles"]]
        return self._tok_cache[key]

    def _match(self, cluster_tokens: list[set[str]], story: dict) -> float:
        best = 0.0
        for a in cluster_tokens:
            for b in self._story_tokens(story):
                if len(a & b) < self.min_shared:
                    continue
                best = max(best, similarity(a, b))
        return best

    def _add_items(self, story: dict, cluster: Cluster) -> None:
        for it in cluster.items:
            ts = it.published or self.now
            if ts > self.now:
                ts = self.now
            name = self.source_name(it.source)
            if name:
                prev = parse_iso(story["sources"].get(name))
                if prev is None or ts < prev:
                    story["sources"][name] = iso(ts)
            if len(story["titles"]) < MAX_TITLES and not any(t["s"] == it.source for t in story["titles"]):
                story["titles"].append({"t": truncate(it.title, 140), "s": it.source,
                                        "u": truncate(it.url or "", 300), "ts": iso(ts)})
        story["last"] = iso(self.now)

    # ── main ──
    def update(self, clusters: list[Cluster]) -> dict[int, dict]:
        """Attach every new cluster to a remembered story (or start a new one)."""
        mapping: dict[int, dict] = {}
        created: list[dict] = []
        ordered = sorted(clusters, key=lambda c: (c.earliest is None, c.earliest or self.now))
        for c in ordered:
            if self.is_routine(c.rep.title):
                continue
            ctoks = [text_tokens(it.title, it.text[:160]) for it in c.items]
            best, best_sim = None, 0.0
            for story in self.stories:
                if story.get("lang") != c.rep.lang:
                    continue
                s = self._match(ctoks, story)
                if s > best_sim:
                    best, best_sim = story, s
            if best is None or best_sim < self.threshold:
                best = {"id": sha(c.rep.uid), "lang": c.rep.lang, "titles": [], "sources": {},
                        "first": iso(c.earliest or self.now), "last": iso(self.now),
                        "trend_at": None, "alerted": False}
                self.stories.append(best)
                created.append(best)
            self._add_items(best, c)
            mapping[id(c)] = best
        self._merge_new(created, mapping)
        return mapping

    def _merge_new(self, created: list[dict], mapping: dict[int, dict]) -> None:
        """Second pass: a story born this run may belong to one that grew later in the run."""
        for story in created:
            if story not in self.stories:
                continue
            toks = self._story_tokens(story)
            for other in self.stories:
                if other is story or other.get("lang") != story.get("lang"):
                    continue
                if self._match(toks, other) >= self.threshold:
                    for name, ts in story["sources"].items():
                        prev = parse_iso(other["sources"].get(name))
                        if prev is None or parse_iso(ts) < prev:
                            other["sources"][name] = ts
                    for t in story["titles"]:
                        if len(other["titles"]) < MAX_TITLES and not any(x["s"] == t["s"] for x in other["titles"]):
                            other["titles"].append(t)
                    other["last"] = iso(self.now)
                    for key, val in list(mapping.items()):
                        if val is story:
                            mapping[key] = other
                    self.stories.remove(story)
                    break

    def candidates(self, clusters: list[Cluster], mapping: dict[int, dict]):
        """Stories touched in this run that just crossed the trend threshold."""
        out = []
        seen_ids = set()
        for c in sorted(clusters, key=lambda c: len(c.items), reverse=True):
            story = mapping.get(id(c))
            if story is None or story["id"] in seen_ids or story.get("trend_at"):
                continue
            seen_ids.add(story["id"])
            times = {s: parse_iso(t) for s, t in story["sources"].items() if parse_iso(t)}
            count, start, end = trend_window(times, self.window)
            if count >= self.min_sources and end is not None and end >= self.now - self.stale:
                minutes = max(1, int((end - start).total_seconds() // 60))
                out.append((c, story, count, minutes))
        out.sort(key=lambda x: x[2], reverse=True)
        return out
