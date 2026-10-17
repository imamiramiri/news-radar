"""Turn model outputs into Telegram HTML message sections."""
from __future__ import annotations

import html

from .utils import esc, fa

URGENCY_ICON = {"فوری": "🔴", "مهم": "🟠", "عادی": "🟢"}
DIRECTION_ICON = {"صعودی": "⬆️", "نزولی": "⬇️", "نوسانی": "↕️", "خنثی": "➖"}


def _s(d: dict, key: str, default: str = "") -> str:
    v = d.get(key, default) if isinstance(d, dict) else default
    return v if isinstance(v, str) else default


def _instagram_block(ig: dict) -> str:
    if not isinstance(ig, dict) or not ig:
        return ""
    lines = ["🎬 <b>پکیج اینستاگرام</b>"]
    if _s(ig, "reel_hook"):
        lines.append(f"قلاب ریلز: «{esc(ig['reel_hook'])}»")
    script = ig.get("reel_script") or []
    if isinstance(script, list) and script:
        lines.append("اسکریپت:")
        lines += [f"{fa(i)}) {esc(b)}" for i, b in enumerate(script, 1)]
    if _s(ig, "caption"):
        lines.append(f"کپشن: {esc(ig['caption'])}")
    poll = ig.get("story_poll") or {}
    if isinstance(poll, dict) and poll.get("question"):
        opts = " / ".join(esc(o) for o in (poll.get("options") or []))
        lines.append(f"استوری: نظرسنجی «{esc(poll['question'])}» ({opts})")
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


def sources_line(cluster) -> str:
    links, seen = [], set()
    for it in cluster.items:
        if it.source in seen:
            continue
        seen.add(it.source)
        if len(links) >= 5:
            continue
        links.append(f'<a href="{html.escape(it.url, quote=True)}">{esc(it.source)}</a>' if it.url else esc(it.source))
    more = len(seen) - len(links)
    tail = f" و {fa(more)} منبع دیگر" if more > 0 else ""
    return "🔗 منابع: " + "، ".join(links) + tail


def alert_sections(result: dict, *, label: str, meta_line: str, sources_html: str = "",
                   verification_note: str = "") -> list[str]:
    urgency = _s(result, "urgency", "مهم")
    icon = URGENCY_ICON.get(urgency, "🟠")
    head = f"{icon} <b>{esc(urgency)}</b> · {esc(label)}\n<b>{esc(_s(result, 'headline'))}</b>"

    body = []
    if _s(result, "why_it_matters"):
        body.append(f"📌 <b>چرا مهمه:</b> {esc(result['why_it_matters'])}")
    angles = result.get("angles") or {}
    angle_lines = [f"• {name}: {esc(angles[key])}" for key, name in
                   (("economic", "اقتصادی"), ("political", "سیاسی"), ("social", "اجتماعی"))
                   if isinstance(angles, dict) and _s(angles, key)]
    if angle_lines:
        body.append("🧭 <b>زاویه‌ها</b>\n" + "\n".join(angle_lines))

    impacts = result.get("market_impact") or []
    if isinstance(impacts, list) and impacts:
        rows = []
        for m in impacts[:5]:
            if not isinstance(m, dict):
                continue
            d = _s(m, "direction")
            rows.append(f"• {esc(_s(m, 'asset'))}: {DIRECTION_ICON.get(d, '•')} {esc(d)} "
                        f"({esc(_s(m, 'horizon'))}، اطمینان {esc(_s(m, 'confidence'))}) — {esc(_s(m, 'note'))}")
        if rows:
            body.append("📊 <b>اثر روی بازار</b>\n" + "\n".join(rows))

    scen = result.get("scenarios") or []
    if isinstance(scen, list) and scen:
        rows = []
        for s in scen[:3]:
            if not isinstance(s, dict):
                continue
            p = s.get("probability")
            p_txt = f" ({fa(p)}٪)" if isinstance(p, (int, float)) else ""
            rows.append(f"• {esc(_s(s, 'name'))}{p_txt}: {esc(_s(s, 'description'))}")
        if rows:
            body.append("🎲 <b>سناریوها</b>\n" + "\n".join(rows))

    if _s(result, "trader_view"):
        body.append(f"🧠 <b>نگاه معامله‌گر:</b> {esc(result['trader_view'])}")

    sections = [head, "\n\n".join(body)]
    ig = _instagram_block(result.get("instagram") or {})
    if ig:
        sections.append(ig)
    tail = []
    if _s(result, "verification"):
        tail.append(f"🔎 اعتبار: {esc(result['verification'])}")
    if verification_note:
        tail.append(f"<blockquote expandable>🔍 راستی‌آزمایی:\n{esc(verification_note)}</blockquote>")
    if sources_html:
        tail.append(sources_html)
    tail.append(f"<i>{esc(meta_line)}</i>")
    sections.append("\n".join(tail))
    return sections


def brief_sections(result: dict, *, title: str, meta_line: str, extra: str = "") -> list[str]:
    head = f"{title}\n<b>{esc(_s(result, 'headline'))}</b>"
    body = []
    watch = result.get("watch_list") or []
    if isinstance(watch, list) and watch:
        body.append("👀 <b>امروز/فردا حواسمون باشه به:</b>\n" + "\n".join(f"• {esc(w)}" for w in watch[:6]))
    sections = [head, "\n\n".join(body)]
    ig = _instagram_block(result.get("instagram") or {})
    if ig:
        sections.append(ig)
    if extra:
        sections.append(extra)
    sections.append(f"<i>{esc(meta_line)}</i>")
    return sections


def calendar_section(ev: dict, minutes_left: int, local_time: str, hint: str) -> list[str]:
    lines = [
        "⏰ <b>یادآوری تقویم اقتصادی</b>",
        f"حدود {fa(minutes_left)} دقیقه دیگه (ساعت {local_time}): <b>{esc(ev['title_fa'])}</b>",
    ]
    fc, pv = ev.get("forecast"), ev.get("previous")
    if fc or pv:
        lines.append(f"پیش‌بینی: {esc(fc or '—')} | قبلی: {esc(pv or '—')}")
    lines.append(f"💡 {esc(hint)}")
    lines.append("<i>بعد از انتشار، اگه عدد سورپرایز باشه، خبرش با تحلیل میاد.</i>")
    return ["\n".join(lines)]
