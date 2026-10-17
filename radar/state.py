"""JSON state persisted between runs (committed back to the repo by GitHub Actions)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from .utils import iso, parse_iso

DEFAULT = {
    "version": 1,
    "seen": {},             # uid -> first-seen iso time
    "sent": [],             # alerts already sent: {ts, headline, category, score}
    "pool": [],             # triaged stories for briefs: {ts, title, source, score, category, sent}
    "pending_quiet": [],    # good stories held back during quiet hours
    "price_history": [],    # {ts, values}
    "market_alert_last": {},
    "briefs": {},           # kind -> local date string
    "calendar": {"fetched_at": None, "events": [], "notified": []},
    "source_health": {},    # label -> {fail_streak, last_ok, last_error}
    "llm_fail_streak": 0,
    "daily": {},            # local date -> {alerts, trends, usd}
    "stories": [],          # recent stories across runs, for trend detection
}


class State:
    def __init__(self, path: Path):
        self.path = Path(path)
        data = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            except json.JSONDecodeError:
                data = {}
        self.data = {**json.loads(json.dumps(DEFAULT)), **data}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # ── seen items ──
    def is_seen(self, uid: str) -> bool:
        return uid in self.data["seen"]

    def mark_seen(self, uids, now: datetime) -> None:
        ts = iso(now)
        for u in uids:
            self.data["seen"].setdefault(u, ts)

    # ── sent alerts ──
    def add_sent(self, headline: str, category: str, score: int, now: datetime) -> None:
        self.data["sent"].append({"ts": iso(now), "headline": headline, "category": category, "score": score})

    def recent_sent(self, now: datetime, hours: float = 24) -> list[str]:
        cut = now - timedelta(hours=hours)
        return [s["headline"] for s in self.data["sent"] if (parse_iso(s["ts"]) or now) >= cut]

    # ── brief pool ──
    def add_pool(self, entry: dict) -> None:
        self.data["pool"].append(entry)

    def pool_since(self, since: datetime) -> list[dict]:
        return [p for p in self.data["pool"] if (parse_iso(p["ts"]) or since) >= since]

    # ── daily counters ──
    def daily(self, local_date: str) -> dict:
        return self.data["daily"].setdefault(local_date, {"alerts": 0, "usd": 0.0})

    # ── source health ──
    def record_health(self, health: dict, now: datetime) -> None:
        sh = self.data["source_health"]
        for label, (ok, _count, err) in health.items():
            rec = sh.setdefault(label, {"fail_streak": 0, "last_ok": None, "last_error": ""})
            if ok:
                rec["fail_streak"] = 0
                rec["last_ok"] = iso(now)
            else:
                rec["fail_streak"] += 1
                rec["last_error"] = err

    def broken_sources(self, min_streak: int = 6) -> list[str]:
        return [k for k, v in self.data["source_health"].items() if v.get("fail_streak", 0) >= min_streak]

    # ── housekeeping ──
    def prune(self, now: datetime) -> None:
        def keep(ts: str | None, hours: float) -> bool:
            t = parse_iso(ts)
            return t is not None and now - t <= timedelta(hours=hours)

        self.data["seen"] = {k: v for k, v in self.data["seen"].items() if keep(v, 72)}
        self.data["sent"] = [s for s in self.data["sent"] if keep(s["ts"], 48)]
        self.data["pool"] = [p for p in self.data["pool"] if keep(p["ts"], 30)]
        self.data["pending_quiet"] = [p for p in self.data["pending_quiet"] if keep(p["ts"], 14)]
        self.data["price_history"] = [p for p in self.data["price_history"] if keep(p["ts"], 30)]
        cal = self.data["calendar"]
        cal["notified"] = cal.get("notified", [])[-200:]
        days = sorted(self.data["daily"])
        for d in days[:-14]:
            self.data["daily"].pop(d, None)
