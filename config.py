"""Load configuration without exposing credentials in diagnostics."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_token: str = field(repr=False)
    allowed_chat_ids: frozenset[int]
    database_path: str = "spesabot.sqlite3"
    gemini_api_key: str = field(default="", repr=False)
    gemini_model: str = "gemini-3.8-flash"

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "Settings":
        load_dotenv(env_file, override=False)
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token or token == "your-telegram-bot-token":
            raise ValueError("Set TELEGRAM_BOT_TOKEN in .env or the environment")
        raw_ids = os.environ.get("ALLOWED_CHAT_IDS", "").split(",")
        if any(not re.fullmatch(r"-?[1-9][0-9]*", value.strip()) for value in raw_ids):
            raise ValueError("ALLOWED_CHAT_IDS must contain comma-separated nonzero chat IDs")
        path = os.environ.get("DATABASE_PATH", "spesabot.sqlite3").strip()
        if not path:
            raise ValueError("DATABASE_PATH must not be empty")
        gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not gemini_key or gemini_key == "your-gemini-api-key":
            raise ValueError("Set GEMINI_API_KEY in .env or the environment")
        gemini_model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"
        return cls(token, frozenset(int(value) for value in raw_ids), path, gemini_key, gemini_model)

