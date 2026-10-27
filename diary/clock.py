"""The owner's local time. One place for the timezone: DIARY_TZ (IANA name), default Europe/Moscow."""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)
MSK_FIXED = timezone(timedelta(hours=3), "MSK")  # Moscow has no DST since 2014


def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name != "Europe/Moscow":
            log.warning("timezone %r not found, falling back to UTC+3", name)
        return MSK_FIXED


TZ: tzinfo = _zone(os.environ.get("DIARY_TZ", "Europe/Moscow"))


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def local_day(when: datetime) -> date:
    """Calendar day of an aware moment (naive values are taken as local time)."""
    return (when if when.tzinfo is None else when.astimezone(TZ)).date()
