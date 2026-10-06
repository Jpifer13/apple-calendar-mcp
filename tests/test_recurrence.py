import pytest

from apple_calendar_mcp.recurrence import RecurrenceError, parse_recurrence


def test_none_passes_through():
    assert parse_recurrence(None) is None


def test_bare_string_is_treated_as_a_frequency():
    spec = parse_recurrence("weekly")
    assert spec.frequency == "weekly"
    assert spec.interval == 1


def test_synonyms_normalize():
    assert parse_recurrence({"frequency": "ANNUALLY"}).frequency == "yearly"
    assert parse_recurrence({"frequency": "day"}).frequency == "daily"


def test_biweekly_implies_an_interval_of_two():
    spec = parse_recurrence({"frequency": "biweekly"})
    assert (spec.frequency, spec.interval) == ("weekly", 2)


def test_explicit_interval_beats_the_implied_one():
    assert parse_recurrence({"frequency": "biweekly", "interval": 3}).interval == 3


def test_weekdays_are_normalized_sorted_and_deduped():
    spec = parse_recurrence({"frequency": "weekly", "days_of_week": ["fri", "Monday", "mon"]})
    assert spec.days_of_week == (0, 4)


def test_weekdays_require_a_weekly_frequency():
    with pytest.raises(RecurrenceError):
        parse_recurrence({"frequency": "monthly", "days_of_week": ["mon"]})


def test_count_and_until_are_mutually_exclusive():
    with pytest.raises(RecurrenceError):
        parse_recurrence({"frequency": "daily", "count": 3, "until": "2026-12-31"})


def test_until_is_parsed_into_a_datetime():
    spec = parse_recurrence({"frequency": "monthly", "until": "2026-12-31"})
    assert spec.until.year == 2026 and spec.until.month == 12


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"frequency": "hourly"},
        {"frequency": "daily", "interval": 0},
        {"frequency": "daily", "interval": "often"},
        {"frequency": "daily", "count": 0},
        {"frequency": "weekly", "days_of_week": "mon"},
        {"frequency": "weekly", "days_of_week": ["funday"]},
        {"frequency": "daily", "until": "whenever"},
    ],
)
def test_malformed_rules_are_rejected(payload):
    with pytest.raises(RecurrenceError):
        parse_recurrence(payload)


def test_describe_reads_naturally():
    spec = parse_recurrence({"frequency": "weekly", "days_of_week": ["tue", "thu"], "count": 4})
    assert spec.describe() == "every week on Tue, Thu for 4 occurrences"
    assert parse_recurrence({"frequency": "biweekly"}).describe() == "every 2 weeks"


def test_to_dict_is_json_friendly():
    payload = parse_recurrence({"frequency": "monthly", "until": "2026-12-31"}).to_dict()
    assert payload["frequency"] == "monthly"
    assert isinstance(payload["until"], str)
    assert payload["count"] is None
