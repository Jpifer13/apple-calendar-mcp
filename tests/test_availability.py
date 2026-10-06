from datetime import datetime, time, timedelta

import pytest

from apple_calendar_mcp.availability import (
    daily_windows,
    find_free_slots,
    merge_intervals,
    subtract,
)
from apple_calendar_mcp.datetimes import local_timezone

TZ = local_timezone()


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A time on 2026-03-{day}. The 2nd is a Monday, the 7th a Saturday."""
    return datetime(2026, 3, day, hour, minute, tzinfo=TZ)


def test_merge_collapses_overlapping_and_touching_intervals():
    merged = merge_intervals(
        [(at(2, 10), at(2, 11)), (at(2, 10, 30), at(2, 12)), (at(2, 12), at(2, 13))]
    )
    assert merged == [(at(2, 10), at(2, 13))]


def test_merge_drops_empty_intervals_and_sorts():
    merged = merge_intervals([(at(2, 15), at(2, 16)), (at(2, 9), at(2, 9)), (at(2, 10), at(2, 11))])
    assert merged == [(at(2, 10), at(2, 11)), (at(2, 15), at(2, 16))]


def test_subtract_carves_a_hole():
    free = subtract((at(2, 9), at(2, 17)), [(at(2, 12), at(2, 13))])
    assert free == [(at(2, 9), at(2, 12)), (at(2, 13), at(2, 17))]


def test_subtract_handles_full_coverage():
    assert subtract((at(2, 9), at(2, 17)), [(at(2, 8), at(2, 18))]) == []


def test_subtract_ignores_busy_outside_the_window():
    window = (at(2, 9), at(2, 17))
    assert subtract(window, [(at(2, 6), at(2, 7)), (at(2, 20), at(2, 22))]) == [window]


def test_daily_windows_respect_working_hours_and_weekdays():
    windows = daily_windows(
        at(2, 0), at(9, 0), between=(time(9), time(17)), weekdays=[0, 1, 2, 3, 4]
    )
    # Mon 2nd through Fri 6th: five windows, skipping the weekend.
    assert len(windows) == 5
    assert windows[0] == (at(2, 9), at(2, 17))
    assert all(start.weekday() < 5 for start, _ in windows)


def test_daily_windows_clip_to_the_requested_range():
    windows = daily_windows(at(2, 14), at(2, 15, 30), between=(time(9), time(17)), weekdays=None)
    assert windows == [(at(2, 14), at(2, 15, 30))]


def test_find_free_slots_skips_gaps_that_are_too_short():
    busy = [(at(2, 9, 30), at(2, 12)), (at(2, 12, 20), at(2, 17))]
    slots = find_free_slots(
        busy, at(2, 0), at(3, 0), minimum=timedelta(minutes=30), between=(time(9), time(17))
    )
    # The 20-minute gap at noon is too short; only the 09:00-09:30 slot survives.
    assert slots == [(at(2, 9), at(2, 9, 30))]


def test_find_free_slots_returns_a_whole_free_day():
    slots = find_free_slots([], at(2, 0), at(3, 0), minimum=timedelta(hours=1), between=(time(9), time(17)))
    assert slots == [(at(2, 9), at(2, 17))]


def test_find_free_slots_honours_the_limit():
    slots = find_free_slots(
        [], at(2, 0), at(7, 0), minimum=timedelta(hours=1), between=(time(9), time(17)), limit=2
    )
    assert len(slots) == 2


def test_find_free_slots_rejects_a_backwards_daily_window():
    with pytest.raises(ValueError):
        find_free_slots([], at(2, 0), at(3, 0), minimum=timedelta(hours=1), between=(time(17), time(9)))


def test_overlapping_busy_blocks_do_not_double_count():
    busy = [(at(2, 10), at(2, 14)), (at(2, 11), at(2, 12))]
    slots = find_free_slots(
        busy, at(2, 0), at(3, 0), minimum=timedelta(minutes=30), between=(time(9), time(17))
    )
    assert slots == [(at(2, 9), at(2, 10)), (at(2, 14), at(2, 17))]


def test_slots_keep_wall_clock_hours_across_a_dst_transition():
    # US DST starts Sunday 2026-03-08; the following Monday must still be 09:00-17:00 local.
    windows = daily_windows(
        datetime(2026, 3, 6, 0, 0, tzinfo=TZ),
        datetime(2026, 3, 10, 23, 59, tzinfo=TZ),
        between=(time(9), time(17)),
        weekdays=[0, 1, 2, 3, 4],
    )
    monday = [w for w in windows if w[0].date().day == 9][0]
    assert monday[0].hour == 9 and monday[1].hour == 17
    assert monday[0].utcoffset() == timedelta(hours=-7)  # now on PDT
