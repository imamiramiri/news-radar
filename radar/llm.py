"""Thin wrapper around the Anthropic Messages API."""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Callable

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


def parse_json(text: str):
    t = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, flags=re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    end = max(t.rfind("}"), t.rfind("]"))
    if starts and end > min(starts):
        return json.loads(t[min(starts): end + 1])
    raise ValueError("no JSON found in model output")


class LLM:
    def __init__(self, pricing: dict | None = None, search_price_per_1k: float = 10.0,
                 mock: bool = False):
        self.mock = mock
        self.pricing = pricing or {}
        self.search_price = search_price_per_1k / 1000.0
        self.usage: dict[str, dict[str, float]] = {}
        self.searches = 0
        self.client = None
        if not mock:
            key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if not key:
                raise LLMError("ANTHROPIC_API_KEY تنظیم نشده است")
            import anthropic  # imported lazily so dry runs work without the package
            self._anthropic = anthropic
            self.client = anthropic.Anthropic(api_key=key, max_retries=3, timeout=180.0)

    # ── accounting ──
    def _record(self, model: str, resp) -> None:
        u = getattr(resp, "usage", None)
        if u is None:
            return
        rec = self.usage.setdefault(model, {"in": 0, "out": 0, "cache_w": 0, "cache_r": 0})
        rec["in"] += getattr(u, "input_tokens", 0) or 0
        rec["out"] += getattr(u, "output_tokens", 0) or 0
        rec["cache_w"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        rec["cache_r"] += getattr(u, "cache_read_input_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        if stu is not None:
            self.searches += getattr(stu, "web_search_requests", 0) or 0

    def cost_usd(self) -> float:
        total = 0.0
        for model, u in self.usage.items():
            p_in, p_out = self.pricing.get(model, [0, 0])
            total += (u["in"] + 1.25 * u["cache_w"] + 0.1 * u["cache_r"]) * p_in / 1e6
            total += u["out"] * p_out / 1e6
        return total + self.searches * self.search_price

    # ── calls ──
    @staticmethod
    def _text(resp) -> str:
        return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")

    def json_call(self, model: str, system: str, user: str, max_tokens: int = 2500,
                  mock: Callable[[], object] | None = None):
        if self.mock:
            return mock() if mock else {}
        last = ""
        for attempt in range(2):
            content = user if attempt == 0 else (
                user + "\n\nمهم: خروجی قبلی JSON معتبر نبود. فقط و فقط یک JSON معتبر برگردان.")
            resp = self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
            )
            self._record(model, resp)
            last = self._text(resp)
            try:
                return parse_json(last)
            except (ValueError, json.JSONDecodeError):
                log.warning("invalid JSON from %s (attempt %d): %.200s", model, attempt + 1, last)
        raise LLMError("مدل JSON معتبر برنگرداند")

    def search_notes(self, model: str, system: str, user: str, max_uses: int,
                     tool_versions: list[str]) -> str:
        """Ask the model to verify a story with the server-side web search tool."""
        if self.mock:
            return "وضعیت: (حالت آزمایشی، جست‌وجو انجام نشد)"
        for version in tool_versions:
            messages = [{"role": "user", "content": user}]
            try:
                for _ in range(3):  # continue if the server pauses a long tool turn
                    resp = self.client.messages.create(
                        model=model, max_tokens=900, system=system, messages=messages,
                        tools=[{"type": version, "name": "web_search", "max_uses": max_uses}],
                    )
                    self._record(model, resp)
                    if resp.stop_reason != "pause_turn":
                        return self._text(resp).strip()
                    messages = messages + [{"role": "assistant",
                                            "content": [b.model_dump(exclude_none=True) for b in resp.content]}]
                return self._text(resp).strip()
            except self._anthropic.BadRequestError as exc:
                log.warning("web search tool %s rejected: %s", version, exc)
                continue
        return ""
