import json

import pytest

from jobsearch.server import VALID_STATUSES, parse_status_request


def body(**payload) -> bytes:
    return json.dumps(payload).encode()


def test_a_valid_request_parses():
    assert parse_status_request(body(id=42, status="applied")) == (42, "applied", None)


def test_a_note_passes_through():
    assert parse_status_request(body(id=42, status="interested", note="ping them")) == (
        42, "interested", "ping them")


def test_every_valid_status_is_accepted():
    for status in VALID_STATUSES:
        assert parse_status_request(body(id=1, status=status))[1] == status


@pytest.mark.parametrize("payload", [
    {"id": 42, "status": "definitely-not-a-status"},
    {"id": 42, "status": "applied; DROP TABLE jobs"},
    {"id": 42, "status": ""},
])
def test_an_unknown_status_is_rejected(payload):
    with pytest.raises(ValueError, match="status"):
        parse_status_request(body(**payload))


@pytest.mark.parametrize("payload", [
    {"id": "not-a-number", "status": "applied"},
    {"id": None, "status": "applied"},
    {"status": "applied"},
])
def test_a_bad_job_id_is_rejected(payload):
    with pytest.raises(ValueError, match="id"):
        parse_status_request(body(**payload))


def test_a_numeric_string_id_is_accepted():
    # JSON from a browser form may carry the id as a string. That is not an error.
    assert parse_status_request(body(id="42", status="skipped")) == (42, "skipped", None)


def test_a_boolean_id_is_rejected():
    # bool is a subclass of int in Python: int(True) == 1. Without an explicit
    # guard, {"id": true} would silently become job id 1.
    with pytest.raises(ValueError, match="id"):
        parse_status_request(body(id=True, status="applied"))


def test_malformed_json_is_rejected():
    with pytest.raises(ValueError):
        parse_status_request(b"{not json")


def test_an_oversized_note_is_rejected_rather_than_truncated_silently():
    with pytest.raises(ValueError, match="note"):
        parse_status_request(body(id=1, status="applied", note="x" * 5000))
