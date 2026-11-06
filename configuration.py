"""Validated numeric settings, shared by startup and import-time constants."""
import math
import os
import re


def number(name: str, default: str, minimum: float, maximum: float, *, integer: bool = True):
    raw = (os.getenv(name) or "").strip() or default
    try:
        value = int(raw) if integer else float(raw)
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError
        return value
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name}: нужно {'целое ' if integer else ''}число от {minimum:g} до {maximum:g}.") from exc


def user_id(value: str) -> bool:
    return bool(re.fullmatch(r"[1-9][0-9]{0,18}", value)) and int(value) <= 2**63 - 1


def validate() -> str | None:
    limits = (
        ("REMIND_CHECK_SEC", "300", 1, 86400, True),
        ("HOUSEKEEPING_SEC", "21600", 1, 604800, True),
        ("REMIND_AFTER_MIN", "180", 1, 525600, True),
        ("RATE_LIMIT_PER_HOUR", "20", 1, 100000, True),
        ("ALERT_THROTTLE_SEC", "3600", 0, 604800, True),
        ("DRAFT_TTL_DAYS", "7", 0, 3650, True),
        ("MIN_CAPTION_CHARS", "8", 1, 4000, True),
        ("PROBATION_SEC", "120", 0, 3600, True),
        ("SHUTDOWN_DRAIN_SEC", "30", 0, 300, False),
        ("MAX_VIDEO_MB", "49", 0.001, 49, False),
        ("MAX_VIDEO_SEC", "180", 1, 3600, True),
        ("MAX_POST_CHARS", "3900", 1, 3900, True),
    )
    try:
        for name, default, low, high, integer in limits:
            number(name, default, low, high, integer=integer)
    except ValueError as exc:
        return exc.args[0]
    return None
