"""Free/busy arithmetic.

Pure interval math over timezone-aware datetimes, independent of EventKit so it
can be tested with hand-written intervals.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .datetimes import local_timezone

Interval = tuple[datetime, datetime]


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Collapse overlapping or touching intervals into a sorted, disjoint list."""
    ordered = sorted((i for i in intervals if i[1] > i[0]), key=lambda i: i[0])
    merged: list[Interval] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def subtract(window: Interval, busy: list[Interval]) -> list[Interval]:
    """Return the parts of ``window`` not covered by any busy interval."""
    free: list[Interval] = []
    cursor, window_end = window
    for busy_start, busy_end in busy:
        if busy_end <= cursor:
            continue
        if busy_start >= window_end:
            break
        if busy_start > cursor:
            free.append((cursor, min(busy_start, window_end)))
        cursor = max(cursor, busy_end)
        if cursor >= window_end:
            return free
    if cursor < window_end:
        free.append((cursor, window_end))
    return free


def daily_windows(
    start: datetime,
    end: datetime,
    *,
    between: tuple[time, time] | None = None,
    weekdays: list[int] | None = None,
) -> list[Interval]:
    """Slice a range into per-day candidate windows.

    ``between`` restricts each day to a wall-clock span (e.g. working hours) and
    ``weekdays`` restricts which days count at all (Monday is 0). Each day's
    window is built in local time, so a day that gains or loses an hour to DST
    still starts and ends at the requested wall-clock times.
    """
    tz = local_timezone()
    windows: list[Interval] = []
    day: date = start.astimezone(tz).date()
    last: date = end.astimezone(tz).date()

    while day <= last:
        if weekdays is None or day.weekday() in weekdays:
            if between is None:
                day_start = datetime.combine(day, time.min, tzinfo=tz)
                day_end = day_start + timedelta(days=1)
            else:
                day_start = datetime.combine(day, between[0], tzinfo=tz)
                day_end = datetime.combine(day, between[1], tzinfo=tz)
            window_start = max(day_start, start)
            window_end = min(day_end, end)
            if window_end > window_start:
                windows.append((window_start, window_end))
        day += timedelta(days=1)
    return windows


def find_free_slots(
    busy: list[Interval],
    start: datetime,
    end: datetime,
    *,
    minimum: timedelta,
    between: tuple[time, time] | None = None,
    weekdays: list[int] | None = None,
    limit: int | None = None,
) -> list[Interval]:
    """Find gaps of at least ``minimum`` that are free of every busy interval."""
    if between is not None and between[1] <= between[0]:
        raise ValueError("The end of the daily window must be after its start.")

    collapsed = merge_intervals(busy)
    slots: list[Interval] = []
    for window in daily_windows(start, end, between=between, weekdays=weekdays):
        for gap_start, gap_end in subtract(window, collapsed):
            if gap_end - gap_start >= minimum:
                slots.append((gap_start, gap_end))
                if limit is not None and len(slots) >= limit:
                    return slots
    return slots
