from datetime import datetime, time

import pytest

from apple_calendar_mcp.datetimes import (
    DateParseError,
    day_start,
    humanize_duration,
    local_timezone,
    parse_clock,
    parse_day,
    parse_weekdays,
    parse_when,
    resolve_event_times,
    resolve_range,
)

TZ = local_timezone()
NOW = datetime(2026, 3, 4, 14, 30, tzinfo=TZ)  # a Wednesday


def test_parses_iso_with_and_without_timezone():
    assert parse_when("2026-03-04T14:30:00-08:00") == NOW
    assert parse_when("2026-03-04T14:30") == NOW
    assert parse_when("2026-03-04 14:30") == NOW


def test_parses_utc_designator():
    parsed = parse_when("2026-03-04T22:30:00Z")
    assert parsed == NOW


def test_date_only_becomes_local_midnight():
    assert parse_when("2026-03-04") == datetime(2026, 3, 4, 0, 0, tzinfo=TZ)


def test_keywords_resolve_against_the_reference_time():
    assert parse_when("now", now=NOW) == NOW
    assert parse_when("today", now=NOW) == datetime(2026, 3, 4, tzinfo=TZ)
    assert parse_when("tomorrow", now=NOW) == datetime(2026, 3, 5, tzinfo=TZ)
    assert parse_when("yesterday", now=NOW) == datetime(2026, 3, 3, tzinfo=TZ)


def test_weekday_name_means_the_next_such_day():
    # NOW is a Wednesday, so "wednesday" means a week out, not today.
    assert parse_when("friday", now=NOW) == datetime(2026, 3, 6, tzinfo=TZ)
    assert parse_when("wednesday", now=NOW) == datetime(2026, 3, 11, tzinfo=TZ)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+90m", datetime(2026, 3, 4, 16, 0, tzinfo=TZ)),
        ("+2h", datetime(2026, 3, 4, 16, 30, tzinfo=TZ)),
        ("-1d", datetime(2026, 3, 3, 14, 30, tzinfo=TZ)),
        ("+3w", datetime(2026, 3, 25, 14, 30, tzinfo=TZ)),
    ],
)
def test_relative_offsets(text, expected):
    assert parse_when(text, now=NOW) == expected


def test_rejects_nonsense():
    with pytest.raises(DateParseError):
        parse_when("next tuesday-ish")
    with pytest.raises(DateParseError):
        parse_when("")
    with pytest.raises(DateParseError):
        parse_when("+5 fortnights")


def test_local_timezone_tracks_daylight_saving():
    # A fixed UTC offset would report the same offset in both months.
    assert day_start(parse_day("2026-01-15")).utcoffset().total_seconds() == -8 * 3600
    assert day_start(parse_day("2026-07-15")).utcoffset().total_seconds() == -7 * 3600


def test_resolve_range_widens_a_date_only_end_to_the_whole_day():
    start, end = resolve_range("2026-03-04", "2026-03-04", now=NOW)
    assert start == datetime(2026, 3, 4, 0, 0, tzinfo=TZ)
    assert end == datetime(2026, 3, 5, 0, 0, tzinfo=TZ)


def test_resolve_range_defaults_to_a_week_from_now():
    start, end = resolve_range(None, None, now=NOW)
    assert start == NOW
    assert (end - start).days == 7


def test_resolve_range_rejects_a_backwards_window():
    with pytest.raises(DateParseError):
        resolve_range("2026-03-10T10:00", "2026-03-04T10:00", now=NOW)


def test_event_times_default_to_one_hour():
    start, end = resolve_event_times("2026-03-04T14:30", None, None, all_day=False, now=NOW)
    assert (end - start).total_seconds() == 3600


def test_event_times_from_duration():
    _, end = resolve_event_times("2026-03-04T14:30", None, 45, all_day=False, now=NOW)
    assert end == datetime(2026, 3, 4, 15, 15, tzinfo=TZ)


def test_event_times_reject_both_end_and_duration():
    with pytest.raises(DateParseError):
        resolve_event_times("2026-03-04T14:30", "2026-03-04T15:00", 45, all_day=False, now=NOW)


def test_relative_end_is_measured_from_the_start_not_from_now():
    start, end = resolve_event_times("2026-03-10T09:00", "+2h", None, all_day=False, now=NOW)
    assert end - start == (end - start)
    assert end == datetime(2026, 3, 10, 11, 0, tzinfo=TZ)


def test_all_day_end_is_the_inclusive_last_day():
    start, end = resolve_event_times("2026-03-10", "2026-03-12", None, all_day=True, now=NOW)
    assert start == datetime(2026, 3, 10, tzinfo=TZ)
    assert end == datetime(2026, 3, 12, tzinfo=TZ)


def test_single_day_all_day_event():
    start, end = resolve_event_times("2026-03-10", None, None, all_day=True, now=NOW)
    assert start == end == datetime(2026, 3, 10, tzinfo=TZ)


def test_all_day_rejects_backwards_span():
    with pytest.raises(DateParseError):
        resolve_event_times("2026-03-12", "2026-03-10", None, all_day=True, now=NOW)


def test_parse_clock_and_weekdays():
    assert parse_clock("09:30") == time(9, 30)
    assert parse_weekdays(["fri", "Monday", "mon"]) == [0, 4]
    with pytest.raises(DateParseError):
        parse_weekdays(["funday"])


def test_humanize_duration():
    assert humanize_duration(5400) == "1h 30m"
    assert humanize_duration(3600) == "1h"
    assert humanize_duration(1800) == "30m"
