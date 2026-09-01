import json
from datetime import datetime

import pytest

from jobsearch.tracker import (
    STAGE_KINDS, build_stats, buckets, parse_answers, parse_application,
    parse_stage_request, parse_transition, parse_when, slug_for,
)

NOW = datetime(2026, 9, 1, 14, 30)


def body(**payload) -> bytes:
    return json.dumps(payload).encode()


def test_a_label_becomes_a_slug():
    assert slug_for("Pair programming round") == "pair_programming_round"
    assert slug_for("  Final / culture interview ") == "final_culture_interview"


def test_a_label_with_no_usable_characters_is_rejected():
    with pytest.raises(ValueError, match="usable"):
        slug_for("!!! ???")


def test_a_custom_stage_parses():
    assert parse_stage_request(body(label="Pair round", weight=45, kind="active")) == {
        "label": "Pair round", "slug": "pair_round", "weight": 45, "kind": "active"}


@pytest.mark.parametrize("payload", [
    {"label": "X", "weight": 101, "kind": "active"},
    {"label": "X", "weight": -1, "kind": "active"},
    {"label": "X", "weight": 50, "kind": "pending"},
    {"label": "", "weight": 50, "kind": "active"},
])
def test_a_bad_stage_is_rejected(payload):
    with pytest.raises(ValueError):
        parse_stage_request(body(**payload))


def test_every_kind_is_accepted():
    for kind in STAGE_KINDS:
        assert parse_stage_request(body(label="X", weight=50, kind=kind))["kind"] == kind


def test_a_transition_carries_a_future_next_action():
    # The whole point: today's move to a stage, with the call booked for the 5th.
    parsed = parse_transition(body(
        stage_id=5, note="scheduled with the CTO",
        next_action="tech interview call", next_action_at="2026-09-05T11:00"), now=NOW)
    assert parsed["stage_id"] == 5
    assert parsed["occurred_at"] == NOW
    assert parsed["next_action_at"] == datetime(2026, 9, 5, 11, 0)


def test_a_transition_can_be_back_dated():
    parsed = parse_transition(body(stage_id=5, occurred_at="2026-08-27"), now=NOW)
    assert parsed["occurred_at"] == datetime(2026, 8, 27, 0, 0)


@pytest.mark.parametrize("value", ["1998-01-01", "2099-01-01", "not-a-date", "2026-13-01"])
def test_an_out_of_range_or_malformed_date_is_rejected(value):
    with pytest.raises(ValueError, match="occurred_at"):
        parse_transition(body(stage_id=5, occurred_at=value), now=NOW)


def test_a_missing_date_is_none_not_an_error():
    assert parse_when(None, "next_action_at", now=NOW) is None
    assert parse_when("", "next_action_at", now=NOW) is None


def test_an_overlong_note_is_rejected():
    with pytest.raises(ValueError, match="note"):
        parse_transition(body(stage_id=5, note="x" * 4001), now=NOW)


def test_answers_are_question_and_answer_pairs():
    assert parse_answers([{"question": "Visa?", "answer": "EU citizen"}]) == [
        {"question": "Visa?", "answer": "EU citizen"}]


@pytest.mark.parametrize("value", [
    [{"question": "", "answer": "x"}],
    [{"question": "q"}],
    "not a list",
    [{"question": "q", "answer": "x"}] * 21,
])
def test_bad_answers_are_rejected(value):
    with pytest.raises(ValueError, match="answers"):
        parse_answers(value)


def test_creating_from_an_existing_job_needs_only_a_job_id():
    parsed = parse_application(body(job_id=42, applied_at="2026-08-30"), now=NOW)
    assert parsed["job_id"] == 42
    assert parsed["posting"] is None
    assert parsed["application"]["applied_at"] == datetime(2026, 8, 30, 0, 0)


def test_creating_from_a_url_carries_the_corrected_posting():
    parsed = parse_application(body(
        url="https://acme.com/careers/42", title="Backend Engineer", company="Acme",
        description="PHP and Postgres.", cover_letter="Dear team,"), now=NOW)
    assert parsed["job_id"] is None
    assert parsed["posting"]["title"] == "Backend Engineer"
    assert parsed["posting"]["url"] == "https://acme.com/careers/42"
    assert parsed["application"]["cover_letter"] == "Dear team,"


@pytest.mark.parametrize("payload", [
    {},                                                   # neither job_id nor url
    {"url": "javascript:alert(1)", "title": "X", "company": "Y"},
    {"url": "https://acme.com/x", "company": "Y"},         # url without a title
])
def test_an_unusable_create_request_is_rejected(payload):
    with pytest.raises(ValueError):
        parse_application(body(**payload), now=NOW)


def test_a_whitespace_only_label_is_rejected():
    # A label that is only whitespace must raise ValueError, not pass required check
    # and then become None downstream.
    with pytest.raises(ValueError, match="label"):
        parse_stage_request(body(label="   ", weight=50, kind="active"))


def test_a_whitespace_only_required_title_is_rejected():
    # Same for required fields in parse_application
    with pytest.raises(ValueError, match="title"):
        parse_application(body(url="https://acme.com/x", title="   ", company="Y"), now=NOW)


@pytest.mark.parametrize("weight_value", [45.7, 45.5, 0.1])
def test_a_float_weight_is_rejected(weight_value):
    # Floats must be rejected to avoid silent rounding.
    with pytest.raises(ValueError, match="weight"):
        parse_stage_request(body(label="X", weight=weight_value, kind="active"))


def test_a_string_integer_weight_is_still_accepted():
    # Numeric strings should still parse as integers (JSON from browser forms).
    result = parse_stage_request(body(label="X", weight="45", kind="active"))
    assert result["weight"] == 45


def test_a_float_string_weight_is_rejected():
    # Strings like "45.7" should not parse as floats and then round.
    with pytest.raises(ValueError, match="weight"):
        parse_stage_request(body(label="X", weight="45.7", kind="active"))


def test_a_stage_id_boolean_is_still_rejected():
    # bool subclasses int, so {"stage_id": true} must still raise ValueError.
    with pytest.raises(ValueError, match="stage_id"):
        parse_transition(body(stage_id=True), now=NOW)


def test_a_timezone_aware_datetime_is_normalized():
    # Asserted as a preserved instant rather than a fixed hour: the correct
    # naive value depends on the machine's zone, but the moment in time it
    # names must not. A bare .replace(tzinfo=None) would shift it and fail here.
    aware = datetime.fromisoformat("2026-09-05T11:00:00+02:00")
    parsed = parse_transition(body(stage_id=5, next_action_at="2026-09-05T11:00:00+02:00"), now=NOW)
    result = parsed["next_action_at"]
    assert result.tzinfo is None
    assert result.astimezone() == aware


APPS = [
    {"id": 1, "applied_at": datetime(2026, 8, 31, 9, 0)},   # current week (Mon)
    {"id": 2, "applied_at": datetime(2026, 8, 27, 9, 0)},   # previous week
    {"id": 3, "applied_at": datetime(2026, 8, 31, 18, 0)},  # current week, no reply
]
EVENTS = [
    {"application_id": 1, "kind": "applied", "occurred_at": datetime(2026, 8, 31, 9, 0),
     "stage_slug": "applied", "stage_kind": "active", "stage_weight": 10},
    {"application_id": 1, "kind": "stage", "occurred_at": datetime(2026, 9, 1, 10, 0),
     "stage_slug": "tech_interview", "stage_kind": "active", "stage_weight": 40},
    {"application_id": 2, "kind": "stage", "occurred_at": datetime(2026, 8, 28, 10, 0),
     "stage_slug": "declined", "stage_kind": "lost", "stage_weight": 0},
    {"application_id": 3, "kind": "applied", "occurred_at": datetime(2026, 8, 31, 18, 0),
     "stage_slug": "applied", "stage_kind": "active", "stage_weight": 10},
]


def test_buckets_are_half_open_and_week_starts_on_monday():
    # 2026-09-01 is a Tuesday.
    windows = buckets(NOW)
    assert windows["current_week"][0] == datetime(2026, 8, 31, 0, 0)
    assert windows["previous_week"] == (datetime(2026, 8, 24), datetime(2026, 8, 31))
    assert windows["previous_day"] == (datetime(2026, 8, 31), datetime(2026, 9, 1))
    assert windows["current_month"][0] == datetime(2026, 9, 1, 0, 0)
    assert windows["previous_month"] == (datetime(2026, 8, 1), datetime(2026, 9, 1))


def test_buckets_handle_a_year_boundary():
    windows = buckets(datetime(2027, 1, 4, 8, 0))     # a Monday
    assert windows["current_week"][0] == datetime(2027, 1, 4, 0, 0)
    assert windows["previous_month"] == (datetime(2026, 12, 1), datetime(2027, 1, 1))


def test_sent_counts_by_applied_at():
    stats = build_stats(APPS, EVENTS, NOW)
    assert stats["current_week"]["sent"] == 2
    assert stats["previous_week"]["sent"] == 1


def test_advanced_counts_moves_into_active_or_won_stages():
    stats = build_stats(APPS, EVENTS, NOW)
    # The tech interview on the 1st, and both 'applied' events on the 31st.
    assert stats["current_week"]["advanced"] == 3
    assert stats["previous_week"]["advanced"] == 0


def test_a_back_dated_event_lands_in_the_week_it_happened():
    stats = build_stats(APPS, EVENTS, NOW)
    assert stats["previous_week"]["lost"] == 1
    assert stats["current_week"]["lost"] == 0


def test_response_rate_is_a_cohort_of_the_bucket_that_was_sent():
    stats = build_stats(APPS, EVENTS, NOW)
    # Of the two sent this week, only #1 ever left "applied".
    assert stats["current_week"]["response_rate"] == 0.5


def test_response_rate_is_none_when_nothing_was_sent():
    # None, not 0.0 — an empty cohort has no rate, and 0% would read as failure.
    assert build_stats([], [], NOW)["previous_day"]["response_rate"] is None


def test_a_ghosting_is_not_a_response():
    events = [{"application_id": 2, "kind": "stage",
               "occurred_at": datetime(2026, 8, 29), "stage_slug": "ghosted",
               "stage_kind": "lost", "stage_weight": 0}]
    assert build_stats([APPS[1]], events, NOW)["previous_week"]["response_rate"] == 0.0
