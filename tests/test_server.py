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


# --- the rewrite-note endpoint carries the same guards as /api/status ---

from jobsearch.drafts import parse_note
from jobsearch.server import host_allowed


def test_note_endpoint_rejects_a_boolean_id_like_the_status_endpoint():
    with pytest.raises(ValueError, match="id"):
        parse_note(body(id=True, note="hi"))


def test_note_endpoint_rejects_malformed_json():
    with pytest.raises(ValueError):
        parse_note(b"{not json")


# --- Host guard: the DNS-rebinding defence, and what --lan widens ------------

def test_loopback_hosts_are_accepted():
    assert host_allowed("127.0.0.1:8765", 8765, False)
    assert host_allowed("localhost:8765", 8765, False)
    assert host_allowed("[::1]:8765", 8765, False)


def test_a_domain_name_is_rejected_even_when_it_resolves_here():
    # This is the whole point of the check. A rebinding attack points a domain
    # the browser trusts at this machine, so the request arrives with the
    # attacker's domain in Host — never a literal address.
    assert not host_allowed("evil.example.com:8765", 8765, False)
    assert not host_allowed("evil.example.com:8765", 8765, True)


def test_a_private_address_needs_lan():
    assert not host_allowed("192.168.1.24:8765", 8765, False)
    assert host_allowed("192.168.1.24:8765", 8765, True)
    assert host_allowed("10.0.0.5:8765", 8765, True)
    assert host_allowed("172.16.3.9:8765", 8765, True)


def test_lan_does_not_open_up_public_addresses():
    # is_private also covers the reserved and documentation ranges, which is
    # wider than "this wifi" but reaches no real host; what matters is that a
    # routable public address is still refused.
    assert not host_allowed("8.8.8.8:8765", 8765, True)
    assert not host_allowed("93.184.216.34:8765", 8765, True)


def test_a_mismatched_port_is_rejected():
    assert not host_allowed("127.0.0.1:9999", 8765, False)


def test_an_absent_host_is_rejected():
    assert not host_allowed("", 8765, True)
