from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else None


@dataclass(frozen=True)
class Config:
    token: str
    owner_id: int | None    # optional: learned from whoever adds the bot to a channel as admin
    channel_id: int | None  # optional: learned from the first channel the bot is added to
    db_path: str
    proxy: str | None
    version: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            token=os.environ["BOT_TOKEN"],
            owner_id=_int("OWNER_ID"),
            channel_id=_int("CHANNEL_ID"),
            db_path=os.environ.get("DIARY_DB", "data/diary.db"),
            proxy=os.environ.get("PROXY_URL") or None,
            version=os.environ.get("APP_VERSION", "dev"),
        )
