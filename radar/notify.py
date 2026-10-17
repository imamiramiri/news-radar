"""Send notifications through a Telegram bot."""
from __future__ import annotations

import logging
import time

import requests

from .utils import strip_tags

log = logging.getLogger(__name__)
LIMIT = 3900  # Telegram hard limit is 4096 characters


class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False):
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.sent = 0
        if not dry_run and (not token or not chat_id):
            raise ValueError("TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID باید تنظیم شوند")

    def _call(self, method: str, payload: dict) -> requests.Response | None:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        for _ in range(4):
            try:
                r = requests.post(url, json=payload, timeout=30)
            except requests.RequestException as exc:
                log.warning("telegram network error: %s", exc)
                time.sleep(3)
                continue
            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(min(int(wait) + 1, 60))
                continue
            return r
        return None

    @staticmethod
    def pack(sections: list[str]) -> list[str]:
        """Pack HTML sections into messages under the length limit without cutting tags."""
        chunks: list[str] = []
        cur = ""
        for sec in sections:
            sec = sec.strip()
            if not sec:
                continue
            if len(sec) > LIMIT:  # a single huge section: send it as plain text pieces
                if cur:
                    chunks.append(cur)
                    cur = ""
                plain = strip_tags(sec)
                chunks.extend(plain[i:i + LIMIT] for i in range(0, len(plain), LIMIT))
                continue
            candidate = f"{cur}\n\n{sec}" if cur else sec
            if len(candidate) > LIMIT:
                chunks.append(cur)
                cur = sec
            else:
                cur = candidate
        if cur:
            chunks.append(cur)
        return chunks

    def send_html(self, sections: list[str]) -> bool:
        ok = True
        for chunk in self.pack(sections):
            ok &= self._send(chunk, html=True)
        return ok

    def send_plain(self, text: str) -> bool:
        ok = True
        for i in range(0, len(text), LIMIT):
            ok &= self._send(text[i:i + LIMIT], html=False)
        return ok

    def _send(self, text: str, html: bool) -> bool:
        if self.dry_run:
            print("\n" + "─" * 60 + ("\n[HTML]\n" if html else "\n[TEXT]\n") + text)
            self.sent += 1
            return True
        payload = {"chat_id": self.chat_id, "text": text,
                   "link_preview_options": {"is_disabled": True}}
        if html:
            payload["parse_mode"] = "HTML"
        r = self._call("sendMessage", payload)
        if r is not None and r.ok:
            self.sent += 1
            time.sleep(1.1)
            return True
        err = r.text[:300] if r is not None else "no response"
        log.warning("telegram send failed: %s", err)
        if html:  # most failures are HTML parse errors: retry as plain text
            return self._send(strip_tags(text), html=False)
        return False

    def get_updates(self) -> list[dict]:
        r = self._call("getUpdates", {"timeout": 0})
        if r is None or not r.ok:
            return []
        return r.json().get("result", [])
