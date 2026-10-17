"""Offline tests: python -m pytest -q   (no internet, no API key needed)."""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar import llm as llm_mod  # noqa: E402
from radar.cluster import cluster_items  # noqa: E402
from radar.config import Config  # noqa: E402
from radar.fetchers import parse_feed, parse_telegram_html  # noqa: E402
from radar.notify import LIMIT, Telegram  # noqa: E402
from radar.pipeline import Radar  # noqa: E402
from radar.utils import in_daily_window  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)  # 12:30 Tehran


class FakeResp:
    def __init__(self, status=200, text="", data=None):
        self.status_code = status
        self.ok = status < 400
        self.text = text
        self.content = text.encode("utf-8")
        self._data = data

    def json(self):
        return self._data if self._data is not None else json.loads(self.text)

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, gold=4100.0, usdt_rial="2350000", event_minutes=40):
        self.gold = gold
        self.usdt_rial = usdt_rial
        self.event_minutes = event_minutes
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        if "t.me/s/akhbarefori" in url:
            return FakeResp(text=(FIX / "telegram_sample.html").read_text(encoding="utf-8"))
        if "t.me/s/" in url:
            return FakeResp(text="<html><body>no posts</body></html>")
        if "news.google.com" in url:
            return FakeResp(text=(FIX / "gnews_sample.xml").read_text(encoding="utf-8"))
        if url == "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml":
            return FakeResp(text=(FIX / "rss_sample.xml").read_text(encoding="utf-8"))
        if "finance.yahoo.com" in url:
            price = self.gold if "GC" in url else 100.0
            return FakeResp(data={"chart": {"result": [{"meta": {"regularMarketPrice": price}}]}})
        if "nobitex" in url:
            return FakeResp(data={"stats": {"usdt-rls": {"latest": self.usdt_rial}}})
        if "faireconomy" in url:
            t = (NOW + timedelta(minutes=self.event_minutes)).isoformat()
            return FakeResp(data=[{"title": "CPI m/m", "country": "USD", "date": t, "impact": "High",
                                   "forecast": "0.3%", "previous": "0.4%"}])
        return FakeResp(status=404, text="not found")


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    shutil.copytree(ROOT / "config", root / "config")
    shutil.copytree(ROOT / "prompts", root / "prompts")
    (root / "state").mkdir(parents=True)
    return root


def make_radar(root, now=NOW, session=None):
    cfg = Config(root)
    tg = Telegram("", "", dry_run=True)
    return Radar(cfg, dry_run=True, mock_llm=True, session=session or FakeSession(), now=now, telegram=tg), tg


# ───────────────────────── unit tests ─────────────────────────

def test_parse_telegram():
    html = (FIX / "telegram_sample.html").read_text(encoding="utf-8")
    items = parse_telegram_html(html, {"username": "akhbarefori", "name": "اخبار فوری"})
    assert len(items) == 4
    first = items[0]
    assert first.uid == "tg:akhbarefori/90001"
    assert "\n" in first.text and "مذاکره" in first.text
    assert first.views == 45200
    assert first.published == datetime(2026, 10, 15, 8, 40, tzinfo=timezone.utc)
    assert items[1].text.startswith("[عکس]")


def test_parse_rss_and_google_news():
    items = parse_feed((FIX / "rss_sample.xml").read_bytes(), {"name": "BBC", "lang": "en"})
    assert len(items) == 3 and items[0].text.startswith("Tehran suspended")
    g = parse_feed((FIX / "gnews_sample.xml").read_bytes(), {"name": "x", "lang": "en"}, "google_news")
    assert g[0].source == "Reuters"
    assert g[0].title == "Iran suspends nuclear talks after fresh US sanctions"


def test_clustering_merges_same_story():
    html = (FIX / "telegram_sample.html").read_text(encoding="utf-8")
    items = parse_telegram_html(html, {"username": "akhbarefori", "name": "اخبار فوری"})
    rss = parse_feed((FIX / "rss_sample.xml").read_bytes(), {"name": "BBC", "lang": "en"})
    g = parse_feed((FIX / "gnews_sample.xml").read_bytes(), {"name": "x", "lang": "en"}, "google_news")
    clusters = cluster_items(items + rss + g, 0.6)
    sizes = sorted(len(c.items) for c in clusters)
    # two dollar posts merge; the BBC and Reuters talks stories merge
    assert sizes.count(2) == 2, [(c.rep.title, len(c.items)) for c in clusters]


def test_quiet_window_over_midnight():
    t = datetime(2026, 1, 1, 2, 0)
    assert in_daily_window(t, "01:30", "07:00")
    assert in_daily_window(datetime(2026, 1, 1, 23, 30), "23:00", "06:00")
    assert not in_daily_window(datetime(2026, 1, 1, 12, 0), "23:00", "06:00")


def test_parse_json_variants():
    assert llm_mod.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm_mod.parse_json('حتما! {"a": [1,2]} تمام') == {"a": [1, 2]}


def test_pack_messages_under_limit():
    sections = ["<b>x</b>" * 300, "y" * 3000, "z" * 5000]
    chunks = Telegram.pack(sections)
    assert all(len(c) <= LIMIT for c in chunks)


# ───────────────────────── end-to-end ─────────────────────────

def test_full_run_sends_alerts_and_reminder(tmp_path, capsys):
    root = make_root(tmp_path)
    radar, tg = make_radar(root)
    stats = radar.run()
    out = capsys.readouterr().out
    assert stats["clusters"] >= 4
    assert stats["alerts"] >= 1
    assert "(آزمایشی)" in out                  # alert card
    assert "یادآوری تقویم اقتصادی" in out       # CPI in 40 minutes
    assert "فوتبال" not in out                  # irrelevant story filtered
    state = json.loads((root / "state" / "state.json").read_text(encoding="utf-8"))
    assert "tg:akhbarefori/80000" in state["seen"]  # too old -> marked seen, never triaged
    assert state["sent"] and state["price_history"]
    vals = state["price_history"][-1]["values"]
    # 4100 $/oz × 235,000 toman / 31.1035 × 0.75 ≈ 23.2 million toman per gram of 18k
    assert 23_000_000 < vals["gold18_intrinsic"] < 23_500_000
    assert "ارزش ذاتی" in radar.market_text

    # second run 30 minutes later: nothing new, no duplicate alerts or reminders
    radar2, _ = make_radar(root, now=NOW + timedelta(minutes=30))
    stats2 = radar2.run()
    out2 = capsys.readouterr().out
    assert stats2["clusters"] == 0 and stats2["alerts"] == 0
    assert "یادآوری تقویم اقتصادی" not in out2


def test_market_move_alert(tmp_path, capsys):
    root = make_root(tmp_path)
    radar, _ = make_radar(root, session=FakeSession(usdt_rial="2350000"))
    radar.run()
    capsys.readouterr()
    # USDT jumps 3% in the next run
    radar2, _ = make_radar(root, now=NOW + timedelta(minutes=30), session=FakeSession(usdt_rial="2420500"))
    radar2.run()
    out = capsys.readouterr().out
    assert "حرکت قیمت" in out and "تتر" in out


def test_quiet_hours_hold_back_and_morning_brief(tmp_path, capsys):
    root = make_root(tmp_path)
    night = datetime(2026, 10, 14, 23, 0, tzinfo=timezone.utc)  # 02:30 Tehran
    session = FakeSession(event_minutes=10_000)
    radar, _ = make_radar(root, now=night, session=session)
    # make fixture items look fresh at night
    radar.cfg.settings["filters"]["lookback_hours"] = 48
    radar.run()
    capsys.readouterr()
    pending = radar.state.data["pending_quiet"]
    assert pending, "non-breaking stories should be held for the morning brief"
    assert all(p["score"] < 88 for p in pending)
    assert all(s["score"] >= 88 for s in radar.state.data["sent"])  # only breaking news at night

    morning = datetime(2026, 10, 15, 4, 40, tzinfo=timezone.utc)  # 08:10 Tehran
    radar2, _ = make_radar(root, now=morning, session=session)
    radar2.run()
    out2 = capsys.readouterr().out
    assert "بریف صبح" in out2
    assert radar2.state.data["pending_quiet"] == []
    assert radar2.state.data["briefs"]["morning"] == "2026-10-15"


# ───────────────────────── trends («ترند») ─────────────────────────
from radar.trends import trend_window  # noqa: E402


def _rss(items) -> str:
    rows = []
    for title, publisher, domain, ts in items:
        rows.append(f"<item><title>{title} - {publisher}</title><link>https://{domain}/n/{abs(hash(title))}</link>"
                    f"<pubDate>{ts.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate>"
                    f"<source url=\"https://{domain}\">{publisher}</source></item>")
    return f"<?xml version='1.0' encoding='UTF-8'?><rss version='2.0'><channel><title>g</title>{''.join(rows)}</channel></rss>"


class NewsSession(FakeSession):
    """Google-News-only fake web: each site returns the items already published at `now`."""

    def __init__(self, articles, now):
        super().__init__(event_minutes=10_000)
        self.articles, self.now = articles, now

    def get(self, url, params=None, timeout=None):
        if "news.google.com" in url:
            hits = [a for a in self.articles if a[2] in url and a[3] <= self.now]
            return FakeResp(text=_rss(hits))
        return FakeResp(status=404, text="not found")


SITES = ["shahraranews.ir", "rouydad24.ir", "khabarfoori.com", "imna.ir", "fararu.com", "tejaratnews.com"]
UTC = timezone.utc
UAE_STORY = [  # today's real headlines, with their real publication times (Tehran = UTC+3:30)
    ("پرواز هواپیمای سلطنتی امارات در آسمان تهران", "شهرآرانیوز", "shahraranews.ir", datetime(2026, 10, 17, 14, 45, tzinfo=UTC)),
    ("گزارش‌ها از حضور یک هواپیمای تشریفاتی اماراتی در تهران", "رویداد۲۴", "rouydad24.ir", datetime(2026, 10, 17, 16, 31, tzinfo=UTC)),
    ("ادعای برخی منابع خبری درباره ورود یک فروند هواپیمای حامل مقامات امارات به تهران", "خبرفوری", "khabarfoori.com", datetime(2026, 10, 17, 16, 35, tzinfo=UTC)),
    ("هواپیمای سلطنتی امارات در تهران", "ایمنا", "imna.ir", datetime(2026, 10, 17, 16, 42, tzinfo=UTC)),
    ("فرود یک هواپیمای تشریفاتی اماراتی در تهران", "فرارو", "fararu.com", datetime(2026, 10, 17, 17, 20, tzinfo=UTC)),
]


def _trend_radar(root, now, articles):
    radar, tg = make_radar(root, now=now, session=NewsSession(articles, now))
    radar.cfg.sources = {"google_news": [{"query": f"site:{d} when:2h", "lang": "fa", "name": d} for d in SITES]}
    return radar


def test_trend_window():
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    times = {"a": t0, "b": t0 + timedelta(minutes=100), "c": t0 + timedelta(minutes=110),
             "d": t0 + timedelta(minutes=125)}
    count, start, end = trend_window(times, timedelta(minutes=60))
    assert count == 3 and start == times["b"] and end == times["d"]


def test_trend_builds_across_runs_and_alerts_once(tmp_path, capsys):
    root = make_root(tmp_path)
    run_times = [datetime(2026, 10, 17, h, m, tzinfo=UTC) for h, m in ((15, 7), (16, 37), (17, 7), (17, 37))]
    outputs = []
    for t in run_times:
        r = _trend_radar(root, t, UAE_STORY)
        stats = r.run()
        outputs.append((stats["trends"], capsys.readouterr().out))
    assert [o[0] for o in outputs] == [0, 0, 1, 0], [o[0] for o in outputs]
    assert "🔥 ترند" in outputs[2][1] and "۳ منبع در ۱۱ دقیقه" in outputs[2][1]
    state = json.loads((root / "state" / "state.json").read_text(encoding="utf-8"))
    uae = [s for s in state["stories"] if "فرارو" in s["sources"]]
    assert len(uae) == 1 and len(uae[0]["sources"]) == 5 and uae[0]["trend_at"]


def test_routine_price_headlines_never_trend(tmp_path, capsys):
    root = make_root(tmp_path)
    base = datetime(2026, 10, 17, 6, 0, tzinfo=UTC)
    routine = [
        ("قیمت دلار امروز شنبه ۲۵ مهر ۱۴۰۵؛ دلار به ۲۶۳ هزار تومان برگشت", "رویداد۲۴", "rouydad24.ir", base),
        ("قیمت دلار امروز ۲۵ مهر ۱۴۰۵ کاهش یافت؛ دلار به ۲۶۳ هزار تومان برگشت", "فرارو", "fararu.com", base + timedelta(minutes=5)),
        ("قیمت دلار امروز شنبه ۲۵ مهر؛ دلار ۲۶۳ هزار تومان برگشت", "تجارت‌نیوز", "tejaratnews.com", base + timedelta(minutes=9)),
    ]
    stats = _trend_radar(root, base + timedelta(minutes=20), routine).run()
    capsys.readouterr()
    assert stats["trends"] == 0
