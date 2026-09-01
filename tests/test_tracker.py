import json
from datetime import datetime

import pytest

from jobsearch.tracker import (
    STAGE_KINDS, parse_answers, parse_application, parse_stage_request,
    parse_transition, parse_when, slug_for,
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
