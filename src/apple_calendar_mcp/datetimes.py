"""Date and time parsing helpers.

Everything here is pure Python so it can be tested without a calendar database
or macOS frameworks. The server accepts ISO 8601 plus a few relative shorthands
("today", "tomorrow", "+3d") because models produce those far more reliably
than they compute calendar arithmetic.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_OFFSET_RE = re.compile(r"^(?P<sign>[+-])(?P<amount>\d+)\s*(?P<unit>[a-z]+)$")

_UNITS = {
    "m": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "minute": "minutes",
    "minutes": "minutes",
    "h": "hours",
    "hr": "hours",
    "hrs": "hours",
    "hour": "hours",
    "hours": "hours",
    "d": "days",
    "day": "days",
    "days": "days",
    "w": "weeks",
    "week": "weeks",
    "weeks": "weeks",
}

WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


class DateParseError(ValueError):
    """Raised when a user-supplied date or time string cannot be understood."""


@lru_cache(maxsize=1)
def local_timezone() -> tzinfo:
    """The machine's local timezone as a DST-aware zone.

    ``datetime.now().astimezone().tzinfo`` would give a *fixed* offset frozen at
    today's DST state, which silently shifts times an hour once a range crosses
    a transition. macOS points /etc/localtime at the zoneinfo file, so the IANA
    name is recoverable; the fixed offset is only a last resort.
    """
    key = os.environ.get("TZ") or _system_timezone_name()
    if key:
        try:
            return ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    tz = datetime.now().astimezone().tzinfo
    assert tz is not None  # astimezone() always attaches one
    return tz


def _system_timezone_name() -> str | None:
    try:
        path = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    marker = "/zoneinfo/"
    index = path.find(marker)
    if index == -1:
        return None
    name = path[index + len(marker):]
    return name or None


def now_local() -> datetime:
    return datetime.now(tz=local_timezone())


def ensure_aware(value: datetime) -> datetime:
    """Attach the local timezone to a naive datetime, leaving aware ones alone."""
    if value.tzinfo is None:
        return value.replace(tzinfo=local_timezone())
    return value


def parse_when(value: str | datetime | date, *, now: datetime | None = None) -> datetime:
    """Parse a point in time.

    Accepts ``datetime``/``date`` objects, ISO 8601 strings (``2026-03-04``,
    ``2026-03-04T14:30``, ``2026-03-04T14:30:00-08:00``), the keywords
    ``now``/``today``/``tomorrow``/``yesterday``, bare weekday names meaning the
    next such day, and relative offsets such as ``+90m``, ``+2h``, ``-1d``,
    ``+3w``. Naive values are interpreted in the machine's local timezone.
    """
    if isinstance(value, datetime):
        return ensure_aware(value)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=local_timezone())
    if not isinstance(value, str):
        raise DateParseError(f"Cannot interpret {value!r} as a date or time.")

    text = value.strip()
    if not text:
        raise DateParseError("Empty date/time string.")

    reference = now or now_local()
    lowered = text.lower()

    if lowered == "now":
        return reference
    if lowered == "today":
        return start_of_day(reference)
    if lowered == "tomorrow":
        return start_of_day(reference) + timedelta(days=1)
    if lowered == "yesterday":
        return start_of_day(reference) - timedelta(days=1)

    if lowered in WEEKDAYS:
        return _next_weekday(start_of_day(reference), WEEKDAYS[lowered])

    offset = _OFFSET_RE.match(lowered)
    if offset:
        unit = _UNITS.get(offset.group("unit"))
        if unit is None:
            raise DateParseError(f"Unknown time unit in {value!r}.")
        amount = int(offset.group("amount"))
        if offset.group("sign") == "-":
            amount = -amount
        return reference + timedelta(**{unit: amount})

    normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
    normalized = normalized.replace(" ", "T", 1) if " " in normalized and "T" not in normalized else normalized
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise DateParseError(
            f"Could not parse {value!r}. Use ISO 8601 (2026-03-04T14:30), a date "
            "(2026-03-04), a keyword (today, tomorrow), or an offset (+2h, +3d)."
        ) from None
    return ensure_aware(parsed)


def parse_day(value: str | datetime | date) -> date:
    """Parse a calendar day, discarding any time component."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return parse_when(value).date()


def parse_clock(value: str) -> time:
    """Parse a wall-clock time of day such as ``09:00`` or ``17:30``."""
    text = value.strip()
    try:
        return time.fromisoformat(text)
    except ValueError:
        raise DateParseError(f"Could not parse {value!r} as a time of day (use HH:MM).") from None


def parse_weekdays(values: list[str] | None) -> list[int] | None:
    """Map weekday names to Python weekday numbers (Monday is 0)."""
    if values is None:
        return None
    days: list[int] = []
    for raw in values:
        key = raw.strip().lower()
        if key not in WEEKDAYS:
            raise DateParseError(f"Unknown weekday {raw!r}.")
        if WEEKDAYS[key] not in days:
            days.append(WEEKDAYS[key])
    return sorted(days)


def start_of_day(value: datetime) -> datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def end_of_day(value: datetime) -> datetime:
    return start_of_day(value) + timedelta(days=1)


def day_start(value: date) -> datetime:
    """Local midnight at the start of a calendar day."""
    return datetime.combine(value, time.min, tzinfo=local_timezone())


def resolve_range(
    start: str | None,
    end: str | None,
    *,
    default_days: int = 7,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Resolve a (start, end) window, filling in sensible defaults.

    A missing start means "right now"; a missing end means ``default_days``
    after the start. A date-only start is widened to the whole day so that
    ``start="today", end="today"`` covers today rather than an empty instant.
    """
    reference = now or now_local()
    begin = parse_when(start, now=reference) if start else reference

    if end:
        finish = parse_when(end, now=reference)
        if _is_date_only(end) or finish == start_of_day(finish):
            finish = end_of_day(finish)
    else:
        finish = begin + timedelta(days=default_days)

    if finish <= begin:
        raise DateParseError(
            f"End ({finish.isoformat()}) must be after start ({begin.isoformat()})."
        )
    return begin, finish


def resolve_event_times(
    start: str,
    end: str | None,
    duration_minutes: int | None,
    *,
    all_day: bool,
    default_minutes: int = 60,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Work out an event's start and end from the arguments a caller supplied.

    For timed events the end comes from ``end``, else ``duration_minutes``, else
    a one-hour default. For all-day events ``end`` is the *inclusive* last day,
    which is how people describe them ("Mon through Wed") and how EventKit
    stores them.
    """
    reference = now or now_local()
    begin = parse_when(start, now=reference)

    if all_day:
        first = begin.date()
        last = parse_day(end) if end else first
        if last < first:
            raise DateParseError("All-day event ends before it starts.")
        return day_start(first), day_start(last)

    if end is not None and duration_minutes is not None:
        raise DateParseError("Pass either end or duration_minutes, not both.")

    if end is not None:
        finish = parse_when(end, now=begin)
    else:
        minutes = default_minutes if duration_minutes is None else duration_minutes
        if minutes <= 0:
            raise DateParseError("duration_minutes must be positive.")
        finish = begin + timedelta(minutes=minutes)

    if finish <= begin:
        raise DateParseError(
            f"End ({finish.isoformat()}) must be after start ({begin.isoformat()})."
        )
    return begin, finish


def to_iso(value: datetime) -> str:
    return ensure_aware(value).isoformat(timespec="seconds")


def humanize_duration(seconds: float) -> str:
    """Render a duration the way a person would say it: ``1h 30m``."""
    minutes = int(round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _next_weekday(reference: datetime, weekday: int) -> datetime:
    delta = (weekday - reference.weekday()) % 7
    return reference + timedelta(days=delta or 7)


def _is_date_only(value: str | datetime | date) -> bool:
    if not isinstance(value, str):
        return isinstance(value, date) and not isinstance(value, datetime)
    text = value.strip().lower()
    if text in {"today", "tomorrow", "yesterday"} or text in WEEKDAYS:
        return True
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text))
