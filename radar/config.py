"""Load settings, sources, prompts and environment variables."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(root: Path, rel: str) -> dict:
    path = root / rel
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_dotenv(root: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(root / ".env")


class Config:
    def __init__(self, root: Path = ROOT):
        self.root = Path(root)
        _load_dotenv(self.root)
        self.settings = _load_yaml(self.root, "config/settings.yaml")
        self.sources = _load_yaml(self.root, "config/sources.yaml")

    def get(self, dotted: str, default=None):
        cur = self.settings
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def prompt(self, name: str) -> str:
        return (self.root / "prompts" / name).read_text(encoding="utf-8")

    def style(self) -> str:
        sig = (self.get("channel.signature") or "").strip()
        sig_text = sig if sig else "(امضا تعریف نشده؛ خط امضا ننویس)"
        return self.prompt("style.md").replace("{{SIGNATURE}}", sig_text)

    def build_prompt(self, name: str, **extra: str) -> str:
        text = self.prompt(name)
        text = text.replace("{{STYLE}}", self.style())
        if "{{ALERT_SCHEMA}}" in text:
            text = text.replace("{{ALERT_SCHEMA}}", self.prompt("alert_schema.md"))
        text = text.replace("{{FOCUS}}", (self.get("channel.focus") or "").strip())
        text = text.replace("{{AVOID}}", (self.get("channel.avoid") or "").strip())
        for key, value in extra.items():
            text = text.replace("{{" + key + "}}", value)
        return text

    @property
    def state_path(self) -> Path:
        return self.root / "state" / "state.json"

    @staticmethod
    def env(name: str, default: str = "") -> str:
        return os.environ.get(name, default).strip()
