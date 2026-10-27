"""Liveness: heartbeat file, polling watchdog, writable data dir."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import GetUpdates

from diary import clock

log = logging.getLogger(__name__)
POLL_FRESH = 180        # the heartbeat is written only while getUpdates succeeded this recently
POLL_DEAD = 15 * 60     # no successful getUpdates this long: exit and let Docker restart us
EXIT_DATA_DIR = 2


class Health:
    """Time of the last successful getUpdates; a grace period equal to the freshness limit after start."""

    def __init__(self) -> None:
        self.last_poll = time.monotonic()

    def polled(self) -> None:
        self.last_poll = time.monotonic()

    def silent_for(self) -> float:
        return time.monotonic() - self.last_poll

    def polling_ok(self) -> bool:
        return self.silent_for() < POLL_FRESH


class PollTracker(BaseRequestMiddleware):
    """aiogram retries getUpdates forever and hides the errors; this records the successful ones."""

    def __init__(self, health: Health) -> None:
        self.health = health

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)  # raises on any API or network error
        if isinstance(method, GetUpdates):
            self.health.polled()
        return result


def heartbeat_path(db_path: Path) -> Path:
    return db_path.parent / "heartbeat"


def write_heartbeat(path: Path, now: datetime | None = None) -> None:
    """Atomic: the healthcheck and the next startup never see a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text((now or clock.now()).isoformat())
    os.replace(tmp, path)


def read_heartbeat(path: Path) -> datetime | None:
    """Time of the last heartbeat; None when the file is missing, empty or garbage."""
    try:
        when = datetime.fromisoformat(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=clock.TZ)


def downtime_minutes(path: Path, now: datetime | None = None) -> float | None:
    last = read_heartbeat(path)
    if last is None:
        return None
    return max(0.0, ((now or clock.now()) - last).total_seconds() / 60)


async def watchdog(bot: Bot, health: Health, alert, limit: float = POLL_DEAD, every: float = 60,
                   exit_fn=os._exit) -> None:
    """Deaf bot (revoked token, 409 conflict, dead proxy) while the process looks alive: restart it."""
    while True:
        await asyncio.sleep(every)
        if health.silent_for() > limit:
            log.error("no successful getUpdates for %d min, exiting for a restart", health.silent_for() // 60)
            try:
                await asyncio.wait_for(alert(f"Не получаю обновления от Telegram уже {int(health.silent_for() // 60)} мин. "
                                             "Перезапускаюсь."), 15)
            except Exception:  # noqa: BLE001 - the alert is best effort
                pass
            exit_fn(1)
            return


def ensure_writable(folder: Path) -> None:
    """Exit with code 2 and a readable message when the data dir is not ours to write."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=folder):
            pass
    except OSError as e:
        uid = os.getuid() if hasattr(os, "getuid") else "?"
        log.error("Data dir %s is not writable for uid %s (%s). Fix: mkdir -p data import && "
                  "sudo chown 1000:1000 data import", folder.resolve(), uid, e)
        sys.exit(EXIT_DATA_DIR)
