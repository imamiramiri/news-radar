"""Market snapshot (global + Iranian prices) and ForexFactory economic calendar.

Every price source is optional: if one fails, the rest of the system keeps working.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import quote

import requests

from .utils import fmt_num, fmt_pct, iso, parse_iso, pct_change, utcnow

log = logging.getLogger(__name__)
TIMEOUT = 15

ASSETS = {
    # key: (Persian label, unit, decimals)
    "usdt_irt": ("تتر (نماینده‌ی دلار آزاد)", "تومان", 0),
    "gold18_intrinsic": ("ارزش ذاتی طلای ۱۸ عیار هر گرم (محاسبه‌ای از انس × تتر)", "تومان", 0),
    "coin_intrinsic": ("ارزش ذاتی سکه امامی (محاسبه‌ای از انس × تتر)", "تومان", 0),
    "gold_ounce": ("انس طلا", "$", 1),
    "brent": ("نفت برنت", "$", 2),
    "dxy": ("شاخص دلار آمریکا (DXY)", "", 2),
    "btc": ("بیت‌کوین", "$", 0),
    "spx_fut": ("آتی S&P 500", "", 0),
}

GRAMS_PER_OUNCE = 31.1035
COIN_EMAMI_GOLD_GRAMS = 8.133 * 0.900  # weight × fineness

YAHOO_SYMBOLS = {"gold_ounce": "GC=F", "brent": "BZ=F", "dxy": "DX-Y.NYB", "btc": "BTC-USD", "spx_fut": "ES=F"}


def _yahoo(session: requests.Session, symbol: str) -> float:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?interval=15m&range=1d"
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    meta = r.json()["chart"]["result"][0]["meta"]
    return float(meta["regularMarketPrice"])


def _gold_api(session: requests.Session) -> float:
    r = session.get("https://api.gold-api.com/price/XAU", timeout=TIMEOUT)
    r.raise_for_status()
    return float(r.json()["price"])


def _coingecko_btc(session: requests.Session) -> float:
    r = session.get("https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "bitcoin", "vs_currencies": "usd"}, timeout=TIMEOUT)
    r.raise_for_status()
    return float(r.json()["bitcoin"]["usd"])


def _nobitex_usdt(session: requests.Session, host: str = "https://api.nobitex.ir") -> float:
    r = session.get(f"{host}/market/stats",
                    params={"srcCurrency": "usdt", "dstCurrency": "rls"}, timeout=TIMEOUT)
    r.raise_for_status()
    rial = float(r.json()["stats"]["usdt-rls"]["latest"])
    return rial / 10.0


def _wallex_usdt(session: requests.Session) -> float:
    r = session.get("https://api.wallex.ir/v1/markets", timeout=TIMEOUT)
    r.raise_for_status()
    return float(r.json()["result"]["symbols"]["USDTTMN"]["stats"]["lastPrice"])


def _first_ok(*fns):
    for fn in fns:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            log.info("price source failed: %s", exc)
    return None


def snapshot(session: requests.Session, now=None) -> dict:
    values: dict[str, float] = {}
    for key, sym in YAHOO_SYMBOLS.items():
        fallbacks = [lambda s=sym: _yahoo(session, s)]
        if key == "gold_ounce":
            fallbacks.append(lambda: _gold_api(session))
        if key == "btc":
            fallbacks.append(lambda: _coingecko_btc(session))
        v = _first_ok(*fallbacks)
        if v is not None:
            values[key] = v
    usdt = _first_ok(lambda: _nobitex_usdt(session),
                     lambda: _nobitex_usdt(session, "https://apiv2.nobitex.ir"),
                     lambda: _wallex_usdt(session))
    if usdt is not None and 10_000 <= usdt <= 10_000_000:  # sanity check (toman)
        values["usdt_irt"] = usdt
    # Derived "intrinsic" values: what gold is worth at today's ounce and dollar, before any bubble.
    if "usdt_irt" in values and "gold_ounce" in values:
        per_gram_24k = values["gold_ounce"] / GRAMS_PER_OUNCE * values["usdt_irt"]
        values["gold18_intrinsic"] = per_gram_24k * 0.75
        values["coin_intrinsic"] = per_gram_24k * COIN_EMAMI_GOLD_GRAMS
    return {"ts": iso(now or utcnow()), "values": values}


def compute_changes(snap: dict, history: list[dict]) -> dict[str, dict]:
    """Change vs the previous snapshot (≤90 min old) and vs ~24h ago."""
    now = parse_iso(snap["ts"])
    prev = None
    day_ago = None
    for h in reversed(history):
        t = parse_iso(h["ts"])
        if t is None or t >= now:
            continue
        if prev is None and now - t <= timedelta(minutes=90):
            prev = h
        if now - t >= timedelta(hours=23):
            day_ago = h
            break
    out: dict[str, dict] = {}
    for key, v in snap["values"].items():
        out[key] = {
            "value": v,
            "prev": (prev or {}).get("values", {}).get(key),
            "chg_short": pct_change(v, (prev or {}).get("values", {}).get(key)),
            "chg_day": pct_change(v, (day_ago or {}).get("values", {}).get(key)),
        }
    return out


def describe(changes: dict[str, dict]) -> str:
    """Plain-text market summary for prompts."""
    if not changes:
        return "داده‌ی قیمت در دسترس نیست."
    lines = []
    for key, (label, unit, dec) in ASSETS.items():
        c = changes.get(key)
        if not c:
            continue
        lines.append(f"- {label}: {fmt_num(c['value'], dec)} {unit} "
                     f"(۳۰ دقیقه: {fmt_pct(c['chg_short'])} | ۲۴ ساعت: {fmt_pct(c['chg_day'])})")
    if not lines:
        return "داده‌ی قیمت در دسترس نیست."
    if "gold18_intrinsic" in changes:
        lines.append("  (ارزش ذاتی = قیمت بدون حباب. اگر قیمت بازار طلا یا سکه در خبرها آمده، "
                     "فاصله‌اش با این عدد همان حباب است.)")
    return "\n".join(lines)


# ─────────────────────────── economic calendar ───────────────────────────

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

EVENT_FA = {
    "CPI m/m": "تورم ماهانه‌ی آمریکا (CPI)",
    "CPI y/y": "تورم سالانه‌ی آمریکا (CPI)",
    "Core CPI m/m": "تورم هسته‌ی ماهانه‌ی آمریکا",
    "Core PCE Price Index m/m": "شاخص قیمت PCE هسته (معیار محبوب فدرال رزرو)",
    "PPI m/m": "تورم تولیدکننده (PPI)",
    "Non-Farm Employment Change": "اشتغال غیرکشاورزی آمریکا (NFP)",
    "Unemployment Rate": "نرخ بیکاری آمریکا",
    "Unemployment Claims": "مدعیان بیکاری هفتگی آمریکا",
    "Federal Funds Rate": "تصمیم نرخ بهره‌ی فدرال رزرو",
    "FOMC Statement": "بیانیه‌ی فدرال رزرو",
    "FOMC Press Conference": "کنفرانس خبری رئیس فدرال رزرو",
    "FOMC Meeting Minutes": "صورتجلسه‌ی فدرال رزرو",
    "Advance GDP q/q": "رشد اقتصادی آمریکا (GDP)",
    "Retail Sales m/m": "خرده‌فروشی آمریکا",
    "ISM Manufacturing PMI": "شاخص مدیران خرید تولیدی (ISM)",
    "ISM Services PMI": "شاخص مدیران خرید خدماتی (ISM)",
}


def event_hint(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ("cpi", "pce", "ppi")):
        return "عدد بالاتر از پیش‌بینی معمولاً یعنی دلار جهانی قوی‌تر و فشار روی انس طلا؛ پایین‌تر، برعکس."
    if any(k in t for k in ("non-farm", "employment", "unemployment", "claims")):
        return "بازار کار قوی معمولاً دلار جهانی رو تقویت و انس طلا رو تضعیف می‌کنه؛ ضعیف، برعکس."
    if any(k in t for k in ("fomc", "federal funds", "fed")):
        return "هر اشاره به کاهش نرخ بهره معمولاً به نفع انس طلا و کریپتوئه؛ لحن سخت‌گیرانه، برعکس."
    return "معمولاً روی دلار جهانی و انس طلا نوسان می‌سازه."


def fetch_calendar(session: requests.Session, currencies: list[str], impacts: list[str]) -> list[dict]:
    r = session.get(FF_URL, timeout=TIMEOUT)
    r.raise_for_status()
    events = []
    for ev in r.json():
        if ev.get("country") not in currencies or ev.get("impact") not in impacts:
            continue
        t = parse_iso(ev.get("date"))
        if t is None:
            continue
        title = ev.get("title", "")
        events.append({
            "id": f"{ev.get('country')}|{title}|{ev.get('date')}",
            "title": title,
            "title_fa": EVENT_FA.get(title, title),
            "currency": ev.get("country"),
            "ts": iso(t),
            "forecast": ev.get("forecast") or "",
            "previous": ev.get("previous") or "",
        })
    return events
