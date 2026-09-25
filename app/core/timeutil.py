from __future__ import annotations

import logging
from datetime import datetime, tzinfo

logger = logging.getLogger(__name__)


def resolve_timezone(configured: str | None) -> tzinfo:
    """Resolves MIKI_TIMEZONE (an IANA name like "Europe/Amsterdam") to a
    tzinfo, or falls back to the system's local timezone when unset or
    invalid. Never raises -- timezone misconfiguration should degrade
    gracefully, not break tool calls."""
    if configured:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(configured)
        except Exception:
            logger.warning("Invalid MIKI_TIMEZONE=%r; falling back to the system local timezone.", configured)

    local_tz = datetime.now().astimezone().tzinfo
    assert local_tz is not None
    return local_tz


def format_current_time(tz: tzinfo) -> str:
    return datetime.now(tz).isoformat(timespec="seconds")


def local_utc_offset() -> str:
    """The system's current UTC offset as ``+HH:MM`` (for building ISO timestamps)."""
    offset = datetime.now().astimezone().strftime("%z")
    if offset and len(offset) == 5:
        return f"{offset[:3]}:{offset[3:]}"
    return "+00:00"
