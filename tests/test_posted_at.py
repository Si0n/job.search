from datetime import datetime, timezone

from jobsearch.adapters.base import parse_posted_at
from jobsearch.adapters.dou import parse_dou_date

TODAY = datetime(2026, 8, 31, 12, 0, 0)


def local(text):
    """The expected naive-local rendering of a tz-aware wire value."""
    return datetime.fromisoformat(text).astimezone().replace(tzinfo=None)


def test_iso_with_offset_becomes_naive_local():
    assert parse_posted_at("2026-08-31T05:04:22+00:00") == local("2026-08-31T05:04:22+00:00")


def test_iso_without_offset_is_taken_as_local():
    assert parse_posted_at("2026-08-27T14:36:09") == datetime(2026, 8, 27, 14, 36, 9)


def test_iso_with_milliseconds_and_z():
    assert parse_posted_at("2026-06-11T08:49:24.689Z") == local("2026-06-11T08:49:24.689+00:00")


def test_rfc_2822_from_the_wordpress_rss_boards():
    assert parse_posted_at("Mon, 31 Aug 2026 09:02:36 +0000") == local("2026-08-31T09:02:36+00:00")


def test_epoch_seconds():
    assert parse_posted_at(1756633462) == datetime.fromtimestamp(1756633462)


def test_epoch_as_a_digit_string():
    assert parse_posted_at("1756633462") == datetime.fromtimestamp(1756633462)


def test_a_bare_year_is_not_read_as_epoch_seconds():
    """'2026' as epoch would date the posting to 1970 and fail every age cut."""
    assert parse_posted_at("2026") is None


def test_a_datetime_passes_through():
    assert parse_posted_at(datetime(2026, 8, 1)) == datetime(2026, 8, 1)


def test_an_aware_datetime_is_converted_not_stripped():
    aware = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    assert parse_posted_at(aware) == aware.astimezone().replace(tzinfo=None)


def test_unparseable_values_are_absent_not_errors():
    for value in (None, "", "   ", "17 серпня", "sometime last week", [], {}):
        assert parse_posted_at(value) is None


def test_booleans_are_not_epoch_seconds():
    assert parse_posted_at(True) is None


# --- DOU: a Ukrainian month name with no year at all ---

def test_dou_date_in_the_current_year():
    assert parse_dou_date("17 серпня", TODAY) == datetime(2026, 8, 17)


def test_dou_date_later_in_the_year_belongs_to_last_year():
    """December read as this December would sit three months in the future and
    read as fresh forever."""
    assert parse_dou_date("15 грудня", TODAY) == datetime(2025, 12, 15)


def test_dou_today_and_yesterday():
    assert parse_dou_date("сьогодні", TODAY) == datetime(2026, 8, 31)
    assert parse_dou_date("вчора", TODAY) == datetime(2026, 8, 30)


def test_dou_impossible_date_is_absent_not_an_error():
    assert parse_dou_date("31 лютого", TODAY) is None


def test_dou_unknown_month_is_absent():
    assert parse_dou_date("17 augustus", TODAY) is None


def test_dou_blank_input():
    assert parse_dou_date(None, TODAY) is None
    assert parse_dou_date("", TODAY) is None
