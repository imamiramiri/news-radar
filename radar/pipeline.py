"""One radar run: fetch → dedupe → triage → verify → write → notify, plus briefs,
market-move alerts and economic-calendar reminders."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import market, mocks
from .cluster import cluster_items
from .config import Config
from .fetchers import fetch_all, make_session
from .formatting import alert_sections, brief_sections, calendar_section, sources_line
from .llm import LLM
from .models import Cluster
from .notify import Telegram
from .state import State
from .utils import (esc, fa, fa_time, fmt_num, hours_ago, in_daily_window, iso, parse_hhmm,
                    parse_iso, to_local, truncate, utcnow)

log = logging.getLogger(__name__)


class Radar:
    def __init__(self, cfg: Config, *, dry_run: bool = False, mock_llm: bool = False,
                 session=None, now: datetime | None = None, telegram: Telegram | None = None):
        self.cfg = cfg
        self.now = now or utcnow()
        self.session = session or make_session()
        self.state = State(cfg.state_path)
        self.mock_llm = mock_llm
        self._llm: LLM | None = None
        self.tg = telegram or Telegram(cfg.env("TELEGRAM_BOT_TOKEN"), cfg.env("TELEGRAM_CHAT_ID"),
                                       dry_run=dry_run)
        self.ctz = cfg.get("time.content_timezone", "Asia/Tehran")
        self.utz = cfg.get("time.user_timezone", self.ctz)
        self.local_now = to_local(self.now, self.ctz)
        self.local_date = self.local_now.date().isoformat()
        self.changes: dict = {}
        self.market_text = "داده‌ی قیمت در دسترس نیست."
        self.events: list[dict] = []
        self.stats = {"items": 0, "clusters": 0, "scored": 0, "alerts": 0}

    # ───────────────────────── helpers ─────────────────────────
    @property
    def llm(self) -> LLM:
        if self._llm is None:
            self._llm = LLM(pricing=self.cfg.get("pricing_usd_per_mtok", {}),
                            search_price_per_1k=float(self.cfg.get("web_search_usd_per_1k", 10)),
                            mock=self.mock_llm)
        return self._llm

    def model(self, role: str) -> str:
        return self.cfg.get(f"models.{role}", "claude-sonnet-5-5")

    def is_quiet(self) -> bool:
        q = self.cfg.get("quiet_hours", {}) or {}
        if not q.get("enabled"):
            return False
        return in_daily_window(to_local(self.now, self.utz), q.get("start", "01:30"), q.get("end", "07:00"))

    def tehran(self, dt: datetime | None) -> str:
        return fa_time(dt or self.now, self.ctz)

    # ───────────────────────── main entry ─────────────────────────
    def run(self) -> dict:
        st = self.state
        st.prune(self.now)
        self.update_market()
        self.update_calendar()
        clusters = self.collect()
        try:
            scored = self.triage(clusters)
            st.data["llm_fail_streak"] = 0
        except Exception as exc:  # noqa: BLE001
            self.on_llm_failure(exc)
            self.finish()
            return self.stats
        for c in clusters:
            st.mark_seen(c.uids, self.now)
        self.add_to_pool(scored)
        st.save()  # checkpoint: never re-triage the same items even if a later step crashes

        self.maybe_briefs()
        self.process_alerts(scored)
        self.market_alerts()
        self.calendar_reminders()
        self.finish()
        return self.stats

    def finish(self) -> None:
        cost = self._llm.cost_usd() if self._llm else 0.0
        daily = self.state.daily(self.local_date)
        daily["usd"] = round(daily.get("usd", 0.0) + cost, 4)
        self.state.save()
        log.info("run done: %s | cost this run ≈ $%.4f | today ≈ $%.3f", self.stats, cost, daily["usd"])

    # ───────────────────────── data collection ─────────────────────────
    def update_market(self) -> None:
        if not self.cfg.get("market_alerts.enabled", True) and not self.cfg.get("briefs.morning.enabled", True):
            return
        try:
            snap = market.snapshot(self.session, now=self.now)
        except Exception as exc:  # noqa: BLE001
            log.warning("market snapshot failed: %s", exc)
            return
        if not snap.get("values"):
            return
        hist = self.state.data["price_history"]
        self.changes = market.compute_changes(snap, hist)
        hist.append(snap)
        self.market_text = market.describe(self.changes)

    def update_calendar(self) -> None:
        if not self.cfg.get("calendar.enabled", True):
            return
        cal = self.state.data["calendar"]
        fetched = parse_iso(cal.get("fetched_at"))
        if fetched is None or self.now - fetched > timedelta(hours=6):
            try:
                cal["events"] = market.fetch_calendar(
                    self.session, self.cfg.get("calendar.currencies", ["USD"]),
                    self.cfg.get("calendar.impacts", ["High"]))
                cal["fetched_at"] = iso(self.now)
            except Exception as exc:  # noqa: BLE001
                log.info("calendar fetch failed: %s", exc)
        self.events = cal.get("events") or []

    def collect(self) -> list[Cluster]:
        items, health = fetch_all(self.cfg.sources, self.session)
        self.state.record_health(health, self.now)
        lookback = float(self.cfg.get("filters.lookback_hours", 3))
        fresh = {}
        for it in items:
            if self.state.is_seen(it.uid) or it.uid in fresh:
                continue
            if it.published and hours_ago(it.published, self.now) > lookback:
                self.state.mark_seen([it.uid], self.now)
                continue
            fresh[it.uid] = it
        clusters = cluster_items(list(fresh.values()), float(self.cfg.get("filters.similarity_threshold", 0.6)))
        limit = int(self.cfg.get("filters.max_clusters_to_triage", 160))
        if len(clusters) > limit:
            clusters.sort(key=lambda c: (c.independent_sources, max(i.weight for i in c.items)), reverse=True)
            for c in clusters[limit:]:
                self.state.mark_seen(c.uids, self.now)
            clusters = clusters[:limit]
        self.stats["items"] = len(fresh)
        self.stats["clusters"] = len(clusters)
        ok = sum(1 for v in health.values() if v[0])
        log.info("fetched %d items from %d/%d sources → %d new stories", len(items), ok, len(health), len(clusters))
        return clusters

    # ───────────────────────── triage ─────────────────────────
    def triage_user(self, chunk: list[Cluster], offset: int, sent: list[str]) -> str:
        lines = [f"زمان فعلی تهران: {self.tehran(self.now)} ({self.local_date})", "",
                 "## داده‌ی بازار", self.market_text, "",
                 "## تیترهای ارسال‌شده در ۲۴ ساعت گذشته (تکرارشون نکن)"]
        lines += [f"- {h}" for h in sent[-40:]] or ["- (هیچ)"]
        lines += ["", "## خبرهای تازه"]
        for i, c in enumerate(chunk):
            rep = c.rep
            when = self.tehran(rep.published) if rep.published else "نامشخص"
            views = f" | بازدید {rep.views:,}" if rep.views else ""
            lines.append(f"[id={offset + i}] منبع: {rep.source} (نوع: {rep.kind}) | ساعت: {when} | "
                         f"تعداد منابع: {len(c.sources)} ({'، '.join(c.sources[:6])}){views}")
            if rep.origin == "telegram":
                lines.append(truncate(rep.text, 450))
            elif rep.text:
                lines.append(f"{rep.title}\n{truncate(rep.text, 300)}")
            else:
                lines.append(rep.title)
            lines.append("")
        return "\n".join(lines)

    def triage(self, clusters: list[Cluster]) -> list[Cluster]:
        if not clusters:
            return []
        system = self.cfg.build_prompt("triage.md")
        sent = self.state.recent_sent(self.now, 24)
        rows: dict[int, dict] = {}
        batch = 40
        for start in range(0, len(clusters), batch):
            chunk = clusters[start:start + batch]
            out = self.llm.json_call(self.model("triage"), system, self.triage_user(chunk, start, sent),
                                     max_tokens=4000, mock=lambda ch=chunk, s=start: mocks.triage(ch, s))
            got = out.get("items") if isinstance(out, dict) else out
            for row in got or []:
                try:
                    rows[int(row["id"])] = row
                except (KeyError, TypeError, ValueError):
                    continue
        scored: list[Cluster] = []
        for idx, c in enumerate(clusters):
            row = rows.get(idx)
            if not row:
                continue
            try:
                c.score = max(0, min(100, int(row.get("score", 0))))
            except (TypeError, ValueError):
                continue
            c.category = str(row.get("category") or "")
            c.assets = [str(a) for a in (row.get("assets") or [])][:6]
            c.novelty = str(row.get("novelty") or "new")
            c.credibility = str(row.get("credibility") or "medium")
            c.reason = str(row.get("reason") or "")
            scored.append(c)
        scored.sort(key=lambda c: c.score, reverse=True)
        self.stats["scored"] = len(scored)
        return scored

    def add_to_pool(self, scored: list[Cluster]) -> None:
        for c in scored:
            if c.score >= 55 and c.novelty != "repeat":
                self.state.add_pool({"ts": iso(self.now), "title": truncate(c.rep.title, 200),
                                     "source": c.rep.source, "score": c.score, "category": c.category})

    # ───────────────────────── news alerts ─────────────────────────
    def process_alerts(self, scored: list[Cluster]) -> None:
        f = self.cfg.get("filters", {}) or {}
        min_s, brk = int(f.get("min_score", 72)), int(f.get("breaking_score", 88))
        per_run, cap = int(f.get("max_alerts_per_run", 4)), int(f.get("daily_alert_cap", 40))
        daily = self.state.daily(self.local_date)
        quiet = self.is_quiet()
        sent_now = 0
        for c in scored:
            if c.score < min_s or c.novelty == "repeat":
                continue
            if sent_now >= per_run or daily["alerts"] >= cap:
                break
            if quiet and c.score < brk:
                self.state.data["pending_quiet"].append({
                    "ts": iso(self.now), "title": truncate(c.rep.title, 200),
                    "source": c.rep.source, "score": c.score, "category": c.category})
                continue
            if self.send_alert(c):
                sent_now += 1
                daily["alerts"] += 1
        self.stats["alerts"] = sent_now

    def verify(self, c: Cluster) -> str:
        v = self.cfg.get("verification", {}) or {}
        if not v.get("enabled", True) or c.score < int(v.get("min_score", 75)):
            return ""
        if v.get("only_single_source", True) and c.independent_sources > 1:
            return ""
        rep = c.rep
        user = (f"خبر برای راستی‌آزمایی (منبع: {rep.source}، ساعت {self.tehran(rep.published)} تهران، "
                f"تاریخ {self.local_date}):\n{rep.title}\n{truncate(rep.text, 800)}\nلینک: {rep.url}")
        try:
            note = self.llm.search_notes(self.model("verify"), self.cfg.prompt("verify.md"), user,
                                         int(v.get("max_searches", 3)),
                                         list(v.get("tool_versions") or ["web_search_20250305"]))
        except Exception as exc:  # noqa: BLE001
            log.warning("verification failed: %s", exc)
            return ""
        return truncate(note, 900)

    def writer_user(self, c: Cluster, verification: str) -> str:
        rep = c.rep
        lines = [
            f"زمان فعلی تهران: {self.tehran(self.now)} ({self.local_date})",
            f"امتیاز غربال: {c.score} | دسته: {c.category} | دارایی‌های مرتبط: {'، '.join(c.assets)} | "
            f"تازگی: {c.novelty} | اعتبار: {c.credibility}",
            f"یادداشت دبیر: {c.reason}", "",
            "## خبر (منبع اصلی)",
            f"منبع: {rep.source} (نوع: {rep.kind}) | ساعت: {self.tehran(rep.published)} | لینک: {rep.url}",
            rep.text if rep.origin == "telegram" else f"{rep.title}\n{rep.text}".strip(),
        ]
        others = [it for it in c.items if it is not rep][:5]
        if others:
            lines += ["", "## گزارش‌های دیگر از همین خبر"]
            lines += [f"- {it.source}: {truncate((it.title + ' — ' + it.text).strip(' —'), 400)}" for it in others]
        if c.only_competitors:
            lines.append("\n⚠️ این خبر فقط از کانال رقیب آمده؛ متنش را کپی نکن و اگر منبع اصلی معلوم نیست، "
                         "آن را تک‌منبعی و تأییدنشده حساب کن.")
        if verification:
            lines += ["", "## نتیجه‌ی راستی‌آزمایی (جست‌وجوی وب)", verification]
        lines += ["", "## داده‌ی بازار", self.market_text]
        today = self.events_on(self.local_now.date())
        if today:
            lines += ["", "## تقویم اقتصادی امروز"] + today
        sent = self.state.recent_sent(self.now, 12)
        if sent:
            lines += ["", "## تیترهای ارسال‌شده‌ی اخیر (برای پیوستگی؛ تکرار نکن)"] + [f"- {h}" for h in sent[-15:]]
        return "\n".join(lines)

    def send_alert(self, c: Cluster) -> bool:
        verification = self.verify(c)
        try:
            result = self.llm.json_call(self.model("writer"), self.cfg.build_prompt("writer.md"),
                                        self.writer_user(c, verification), max_tokens=3000,
                                        mock=lambda: mocks.writer(c.rep.title))
        except Exception as exc:  # noqa: BLE001
            log.error("writer failed for %s: %s", c.rep.title[:60], exc)
            return False
        if not isinstance(result, dict):
            return False
        if result.get("should_post") is False:
            log.info("writer skipped '%s': %s", c.rep.title[:60], result.get("skip_reason"))
            return False
        meta = (f"⏱ {self.tehran(c.earliest)} تهران · {fa(len(c.sources))} منبع · "
                f"{c.reason}")
        sections = alert_sections(result, label=f"امتیاز {fa(c.score)} · {c.category or 'خبر'}",
                                  meta_line=meta, sources_html=sources_line(c),
                                  verification_note=verification)
        return self._deliver(sections, result, c.category, c.score, fallback_headline=c.rep.title)

    def _deliver(self, sections: list[str], result: dict, category: str, score: int,
                 fallback_headline: str) -> bool:
        post = result.get("telegram_post") if isinstance(result.get("telegram_post"), str) else ""
        separate = bool(self.cfg.get("telegram.send_ready_post_separately", True))
        if post and not separate:
            sections = sections[:1] + [f"📝 <b>متن آماده:</b>\n<blockquote>{esc(post)}</blockquote>"] + sections[1:]
        ok = self.tg.send_html(sections)
        if ok and post and separate:
            self.tg.send_plain(post)
        if ok:
            self.state.add_sent(result.get("headline") or fallback_headline, category, score, self.now)
        return ok

    # ───────────────────────── market moves ─────────────────────────
    def market_alerts(self) -> None:
        if not self.cfg.get("market_alerts.enabled", True) or not self.changes:
            return
        th = self.cfg.get("market_alerts.thresholds_pct", {}) or {}
        cooldown = timedelta(minutes=int(self.cfg.get("market_alerts.cooldown_minutes", 120)))
        last = self.state.data["market_alert_last"]
        for key, c in self.changes.items():
            limit, chg = th.get(key), c.get("chg_short")
            if limit is None or chg is None or abs(chg) < float(limit):
                continue
            lt = parse_iso(last.get(key))
            if lt and self.now - lt < cooldown:
                continue
            if self.is_quiet() and abs(chg) < 2 * float(limit):
                continue
            if self.send_market_alert(key, c):
                last[key] = iso(self.now)

    def send_market_alert(self, key: str, c: dict) -> bool:
        label, unit, dec = market.ASSETS[key]
        recent = sorted(self.state.pool_since(self.now - timedelta(hours=6)),
                        key=lambda p: p["score"], reverse=True)[:25]
        news = "\n".join(f"- [{p['source']}] {p['title']} (امتیاز {p['score']})" for p in recent) or "- (خبر مهمی ثبت نشده)"
        user = (f"دارایی: {label}\nقیمت حدود ۳۰ دقیقه پیش: {fmt_num(c['prev'], dec)} {unit}\n"
                f"قیمت فعلی: {fmt_num(c['value'], dec)} {unit}\n"
                f"تغییر کوتاه‌مدت: {c['chg_short']:+.2f}%\n"
                f"تغییر ۲۴ ساعته: {('%+.2f%%' % c['chg_day']) if c.get('chg_day') is not None else 'نامشخص'}\n\n"
                f"## کل داده‌ی بازار\n{self.market_text}\n\n## خبرهای مهم ۶ ساعت اخیر\n{news}\n\n"
                f"زمان فعلی تهران: {self.tehran(self.now)}")
        try:
            result = self.llm.json_call(self.model("writer"), self.cfg.build_prompt("market_move.md"), user,
                                        max_tokens=2500, mock=lambda: mocks.writer(f"حرکت {label}"))
        except Exception as exc:  # noqa: BLE001
            log.error("market alert failed: %s", exc)
            return False
        if not isinstance(result, dict) or result.get("should_post") is False:
            return False
        sign = "📈" if c["chg_short"] > 0 else "📉"
        chg_txt = fa(f"{c['chg_short']:+.1f}")
        sections = alert_sections(result, label=f"{sign} حرکت قیمت · {label} {chg_txt}٪",
                                  meta_line=f"⏱ {self.tehran(self.now)} تهران · هشدار خودکار قیمت")
        return self._deliver(sections, result, "حرکت قیمت", 90, fallback_headline=f"حرکت {label}")

    # ───────────────────────── calendar ─────────────────────────
    def events_on(self, day) -> list[str]:
        out = []
        for ev in self.events:
            t = parse_iso(ev["ts"])
            if t and to_local(t, self.ctz).date() == day:
                out.append(f"- ساعت {self.tehran(t)}: {ev['title_fa']} (پیش‌بینی {ev.get('forecast') or '—'}، "
                           f"قبلی {ev.get('previous') or '—'})")
        return out

    def calendar_reminders(self) -> None:
        if not self.cfg.get("calendar.enabled", True) or self.is_quiet():
            return
        heads = int(self.cfg.get("calendar.heads_up_minutes", 60))
        notified = self.state.data["calendar"].setdefault("notified", [])
        for ev in self.events:
            t = parse_iso(ev["ts"])
            if t is None or ev["id"] in notified:
                continue
            mins = (t - self.now).total_seconds() / 60
            if 0 < mins <= heads:
                if self.tg.send_html(calendar_section(ev, int(mins), self.tehran(t), market.event_hint(ev["title"]))):
                    notified.append(ev["id"])

    # ───────────────────────── briefs ─────────────────────────
    def brief_due(self, kind: str) -> bool:
        b = self.cfg.get(f"briefs.{kind}", {}) or {}
        if not b.get("enabled", True):
            return False
        t = parse_hhmm(b.get("time", "08:00" if kind == "morning" else "21:30"))
        target = self.local_now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        window = timedelta(minutes=int(self.cfg.get("briefs.window_minutes", 75)))
        if not (target <= self.local_now < target + window):
            return False
        return self.state.data["briefs"].get(kind) != self.local_date

    def maybe_briefs(self, force: str | None = None) -> None:
        for kind in ("morning", "evening"):
            if force is not None and kind != force:
                continue
            if force is None and not self.brief_due(kind):
                continue
            self.send_brief(kind)

    def send_brief(self, kind: str) -> bool:
        morning = kind == "morning"
        since = self.now - timedelta(hours=12 if morning else 14)
        pool = sorted(self.state.pool_since(since), key=lambda p: p["score"], reverse=True)[:30]
        pending = self.state.data["pending_quiet"] if morning else []
        day = self.local_now.date() if morning else (self.local_now + timedelta(days=1)).date()
        events = self.events_on(day)
        lines = [f"زمان فعلی تهران: {self.tehran(self.now)} ({self.local_date})", "",
                 "## داده‌ی بازار", self.market_text, "",
                 f"## خبرهای مهم {'از دیشب تا الان' if morning else 'امروز'} (به ترتیب اهمیت)"]
        lines += [f"- [{p['source']}] {p['title']} (امتیاز {p['score']}، {p['category']})" for p in pool] or ["- (خبر مهمی ثبت نشده)"]
        if pending:
            lines += ["", "## خبرهایی که در ساعت سکوت نگه داشته شدند"]
            lines += [f"- [{p['source']}] {p['title']} (امتیاز {p['score']})" for p in pending[-20:]]
        lines += ["", f"## تقویم اقتصادی {'امروز' if morning else 'فردا'}"] + (events or ["- (رویداد مهمی ثبت نشده)"])
        sent = self.state.recent_sent(self.now, 14)
        if sent:
            lines += ["", "## تیترهایی که قبلاً جداگانه پوشش دادیم"] + [f"- {h}" for h in sent[-20:]]
        name = "بریف صبح" if morning else "جمع‌بندی شب"
        try:
            result = self.llm.json_call(self.model("brief"), self.cfg.build_prompt("brief.md", KIND=name),
                                        "\n".join(lines), max_tokens=2500, mock=lambda: mocks.brief(name))
        except Exception as exc:  # noqa: BLE001
            log.error("brief failed: %s", exc)
            return False
        if not isinstance(result, dict):
            return False
        extra = ""
        if not morning:
            daily = self.state.daily(self.local_date)
            cost = daily.get("usd", 0.0) + (self._llm.cost_usd() if self._llm else 0.0)
            extra_lines = [f"📟 <b>گزارش رادار:</b> {fa(daily.get('alerts', 0))} هشدار خبری امروز · "
                           f"هزینه‌ی تقریبی API امروز: ${cost:.2f}"]
            broken = self.state.broken_sources()
            if broken:
                extra_lines.append("⚠️ منابعی که چند ساعته کار نمی‌کنن: " + "، ".join(esc(b) for b in broken[:10]))
            extra = "\n".join(extra_lines)
        title = "☀️ <b>بریف صبح</b>" if morning else "🌙 <b>جمع‌بندی شب</b>"
        sections = brief_sections(result, title=title, meta_line=f"⏱ {self.tehran(self.now)} تهران", extra=extra)
        ok = self.tg.send_html(sections)
        post = result.get("telegram_post")
        if ok and isinstance(post, str) and post:
            self.tg.send_plain(post)
        if ok:
            self.state.data["briefs"][kind] = self.local_date
            if morning:
                self.state.data["pending_quiet"] = []
        return ok

    # ───────────────────────── failures ─────────────────────────
    def on_llm_failure(self, exc: Exception) -> None:
        st = self.state.data
        st["llm_fail_streak"] = st.get("llm_fail_streak", 0) + 1
        log.error("triage failed (%d in a row): %s", st["llm_fail_streak"], exc)
        if st["llm_fail_streak"] == 3 and self.cfg.get("telegram.send_errors", True):
            self.tg.send_html([
                "⚠️ <b>رادار خبر:</b> سه بار پشت سر هم ارتباط با Claude API خطا داد.\n"
                f"<code>{esc(str(exc))[:300]}</code>\n"
                "اعتبار حساب (Billing) و کلید API رو در platform.claude.com چک کنید."])
