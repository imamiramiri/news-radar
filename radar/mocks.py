"""Deterministic fake model outputs, used by --mock-llm dry runs and tests."""
from __future__ import annotations

KEYWORDS = {
    "دلار": 25, "طلا": 18, "سکه": 12, "تتر": 12, "مذاکره": 22, "تحریم": 18, "ترامپ": 15,
    "بنزین": 18, "بورس": 10, "تورم": 15, "iran": 25, "oil": 12, "fed": 18, "gold": 12,
    "hormuz": 25, "sanction": 18, "inflation": 15, "bitcoin": 8,
}


def triage(chunk, offset: int) -> dict:
    rows = []
    for i, c in enumerate(chunk):
        text = (c.rep.title + " " + c.rep.text).lower()
        score = 40 + sum(v for k, v in KEYWORDS.items() if k in text) + 5 * (len(c.sources) - 1)
        score = max(0, min(97, score))
        if score >= 50:
            rows.append({"id": offset + i, "score": score, "category": "ارز و طلا", "assets": ["دلار"],
                         "novelty": "new", "credibility": "medium", "reason": "آزمایشی: کلیدواژه‌های مهم"})
    return {"items": rows}


def writer(title: str) -> dict:
    return {
        "urgency": "مهم",
        "headline": f"(آزمایشی) {title[:60]}",
        "telegram_post": f"این یک متن آزمایشیه برای: {title[:80]}\nپ.ن: در حالت واقعی، Claude این‌جا پست می‌نویسه.",
        "why_it_matters": "در حالت آزمایشی تحلیلی تولید نمی‌شود.",
        "angles": {"economic": "آزمایشی", "political": "", "social": ""},
        "market_impact": [{"asset": "دلار آزاد", "direction": "نوسانی", "horizon": "امروز",
                           "confidence": "کم", "note": "آزمایشی"}],
        "scenarios": [{"name": "سناریوی اصلی", "probability": 60, "description": "آزمایشی"},
                      {"name": "سناریوی جایگزین", "probability": 40, "description": "آزمایشی"}],
        "trader_view": "آزمایشی",
        "instagram": {"reel_hook": "آزمایشی؟", "reel_script": ["۱", "۲", "۳"], "caption": "آزمایشی",
                      "story_poll": {"question": "آزمایشی؟", "options": ["آره", "نه"]}},
        "verification": "تک‌منبعی - با احتیاط",
        "should_post": True,
        "skip_reason": "",
    }


def brief(kind: str) -> dict:
    return {
        "headline": f"(آزمایشی) {kind}",
        "telegram_post": f"▫️ این {kind} آزمایشیه\n▫️ در حالت واقعی، Claude این‌جا خلاصه می‌نویسه.",
        "watch_list": ["مورد آزمایشی ۱", "مورد آزمایشی ۲"],
        "instagram": {"reel_hook": "آزمایشی", "caption": "آزمایشی",
                      "story_poll": {"question": "آزمایشی؟", "options": ["آره", "نه"]}},
    }
