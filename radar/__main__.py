"""Command-line entry point.

  python -m radar run                 # one full cycle (what the scheduler runs every 30 minutes)
  python -m radar run --dry-run --mock-llm   # no API calls, no Telegram: print to the console
  python -m radar check-sources       # test every source and show which ones work
  python -m radar test-telegram       # send a test message to your chat
  python -m radar chat-id             # find your chat id after sending /start to the bot
  python -m radar brief morning       # force a morning (or evening) brief now
  python -m radar prices              # show the current market snapshot
"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import Config
from .fetchers import fetch_all, make_session, source_jobs
from .notify import Telegram


def cmd_check_sources(cfg: Config) -> int:
    jobs = source_jobs(cfg.sources)
    print(f"در حال تست {len(jobs)} منبع...\n")
    _items, health = fetch_all(cfg.sources, make_session())
    ok = 0
    for label in sorted(health, key=lambda k: (not health[k][0], k)):
        good, count, err = health[label]
        ok += good
        mark = "✅" if good else "❌"
        detail = f"{count} مورد" if good else err
        print(f"{mark} {label}: {detail}")
    print(f"\n{ok} از {len(health)} منبع کار می‌کنن.")
    return 0


def cmd_test_telegram(cfg: Config) -> int:
    tg = Telegram(cfg.env("TELEGRAM_BOT_TOKEN"), cfg.env("TELEGRAM_CHAT_ID"))
    ok = tg.send_html(["✅ <b>رادار خبر وصله!</b>\nاز این به بعد خبرهای مهم این‌جا میاد."])
    print("پیام تست ارسال شد." if ok else "ارسال ناموفق بود؛ توکن و chat id رو چک کنید.")
    return 0 if ok else 1


def cmd_chat_id(cfg: Config) -> int:
    token = cfg.env("TELEGRAM_BOT_TOKEN")
    if not token:
        print("اول TELEGRAM_BOT_TOKEN رو تنظیم کنید.")
        return 1
    tg = Telegram(token, "0")
    updates = tg.get_updates()
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
    if not chats:
        print("هیچ پیامی پیدا نشد. اول در تلگرام به ربات‌تون /start بفرستید و دوباره اجرا کنید.")
        return 1
    print("chat id های پیدا شده:")
    for cid, name in chats.items():
        print(f"  TELEGRAM_CHAT_ID = {cid}    ({name})")
    return 0


def cmd_prices(cfg: Config) -> int:
    from . import market
    snap = market.snapshot(make_session())
    print(market.describe(market.compute_changes(snap, [])))
    missing = [k for k in market.ASSETS if k not in snap["values"]]
    if missing:
        print("\nدر دسترس نبود:", ", ".join(missing))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radar", description="رادار خبر اقتصادی")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run")
    run.add_argument("--dry-run", action="store_true", help="به‌جای تلگرام در کنسول چاپ کن")
    run.add_argument("--mock-llm", action="store_true", help="بدون صدا زدن Claude (برای تست)")
    sub.add_parser("check-sources")
    sub.add_parser("test-telegram")
    sub.add_parser("chat-id")
    sub.add_parser("prices")
    brief = sub.add_parser("brief")
    brief.add_argument("kind", choices=["morning", "evening"])
    brief.add_argument("--dry-run", action="store_true")
    brief.add_argument("--mock-llm", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "urllib3", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = Config()

    if args.command in (None, "run"):
        from .pipeline import Radar
        radar = Radar(cfg, dry_run=getattr(args, "dry_run", False), mock_llm=getattr(args, "mock_llm", False))
        radar.run()
        return 0
    if args.command == "check-sources":
        return cmd_check_sources(cfg)
    if args.command == "test-telegram":
        return cmd_test_telegram(cfg)
    if args.command == "chat-id":
        return cmd_chat_id(cfg)
    if args.command == "prices":
        return cmd_prices(cfg)
    if args.command == "brief":
        from .pipeline import Radar
        radar = Radar(cfg, dry_run=args.dry_run, mock_llm=args.mock_llm)
        radar.update_market()
        radar.update_calendar()
        radar.maybe_briefs(force=args.kind)
        radar.finish()
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
