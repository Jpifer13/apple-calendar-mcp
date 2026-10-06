"""Normalization of repeat rules into a backend-neutral shape.

Kept free of EventKit imports so the validation rules can be tested directly;
``store.py`` turns a :class:`RecurrenceSpec` into an ``EKRecurrenceRule``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .datetimes import DateParseError, parse_when, parse_weekdays, to_iso

FREQUENCIES = {
    "daily": "daily",
    "day": "daily",
    "weekly": "weekly",
    "week": "weekly",
    "biweekly": "weekly",
    "fortnightly": "weekly",
    "monthly": "monthly",
    "month": "monthly",
    "yearly": "yearly",
    "year": "yearly",
    "annually": "yearly",
}

# "every other week" is a frequency *and* an interval; remember the implied one.
_IMPLIED_INTERVAL = {"biweekly": 2, "fortnightly": 2}


class RecurrenceError(ValueError):
    """Raised when a repeat rule is malformed or self-contradictory."""


@dataclass(frozen=True)
class RecurrenceSpec:
    frequency: str
    interval: int = 1
    days_of_week: tuple[int, ...] = ()
    count: int | None = None
    until: datetime | None = None

    def describe(self) -> str:
        every = "every" if self.interval == 1 else f"every {self.interval}"
        unit = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}[
            self.frequency
        ]
        parts = [f"{every} {unit}" if self.interval == 1 else f"{every} {unit}s"]
        if self.days_of_week:
            names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            parts.append("on " + ", ".join(names[d] for d in self.days_of_week))
        if self.count:
            parts.append(f"for {self.count} occurrences")
        elif self.until:
            parts.append(f"until {to_iso(self.until)}")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "frequency": self.frequency,
            "interval": self.interval,
            "days_of_week": list(self.days_of_week),
            "count": self.count,
            "until": to_iso(self.until) if self.until else None,
            "description": self.describe(),
        }


def parse_recurrence(raw: dict[str, Any] | str | None) -> RecurrenceSpec | None:
    """Build a :class:`RecurrenceSpec` from a dict (or a bare frequency string)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = {"frequency": raw}
    if not isinstance(raw, dict):
        raise RecurrenceError("recurrence must be an object or a frequency name.")

    frequency_raw = str(raw.get("frequency", "")).strip().lower()
    if not frequency_raw:
        raise RecurrenceError("recurrence.frequency is required.")
    if frequency_raw not in FREQUENCIES:
        raise RecurrenceError(
            f"Unknown frequency {frequency_raw!r}. "
            f"Use one of: {', '.join(sorted(set(FREQUENCIES.values())))}."
        )
    frequency = FREQUENCIES[frequency_raw]

    interval = raw.get("interval")
    if interval is None:
        interval = _IMPLIED_INTERVAL.get(frequency_raw, 1)
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        raise RecurrenceError("recurrence.interval must be a whole number.") from None
    if interval < 1:
        raise RecurrenceError("recurrence.interval must be at least 1.")

    days = raw.get("days_of_week")
    if days is not None and not isinstance(days, list):
        raise RecurrenceError("recurrence.days_of_week must be a list of weekday names.")
    try:
        weekdays = parse_weekdays(days) or []
    except DateParseError as exc:
        raise RecurrenceError(str(exc)) from None
    if weekdays and frequency != "weekly":
        raise RecurrenceError("recurrence.days_of_week only applies to weekly repeats.")

    count = raw.get("count")
    until_raw = raw.get("until")
    if count is not None and until_raw is not None:
        raise RecurrenceError("Pass either recurrence.count or recurrence.until, not both.")

    if count is not None:
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise RecurrenceError("recurrence.count must be a whole number.") from None
        if count < 1:
            raise RecurrenceError("recurrence.count must be at least 1.")

    until = None
    if until_raw is not None:
        try:
            until = parse_when(until_raw)
        except DateParseError as exc:
            raise RecurrenceError(f"recurrence.until: {exc}") from None

    return RecurrenceSpec(
        frequency=frequency,
        interval=interval,
        days_of_week=tuple(weekdays),
        count=count,
        until=until,
    )
