import gzip
import json
import pathlib
from datetime import datetime

from jobsearch import salary
from jobsearch.adapters.base import JsonAdapter
from jobsearch.adapters.remoteok import RemoteOkAdapter
from jobsearch.adapters.weworkremotely import WeWorkRemotelyAdapter
from jobsearch.models import RawFetch, RawPosting

SELECTORS = {
    "root": "",
    "skip_first": True,
    "fields": {
        "external_id": "id",
        "url": "url",
        "title": "position",
        "company": "company",
        "location": "location",
        "salary_raw": "salary",
        "description": "description",
        "arrangement_hint": "arrangement",
    },
}

FEED = json.dumps([
    {"legal": "this first element is a legal notice, not a job"},
    {"id": "1001", "url": "https://remoteok.com/l/1001", "position": "Senior PHP Developer",
     "company": "Acme", "location": "Worldwide", "salary": "$70,000 - $90,000",
     "description": "<p>Laravel.</p>", "arrangement": "Remote"},
    {"id": "1002", "url": "https://remoteok.com/l/1002", "position": "Backend Engineer",
     "company": "Globex", "location": "Europe", "description": "<p>Go.</p>"},
]).encode()

EMPTY = json.dumps([{"legal": "notice"}]).encode()
NOT_JSON = b"<html>rate limited</html>"
WRONG_SHAPE = json.dumps({"error": "unauthorized"}).encode()


class _Probe(JsonAdapter):
    name = "probe"
    base_url = "https://example.test"

    def build_url(self, query):
        return self.base_url


def _raw(body: bytes) -> RawFetch:
    return RawFetch("probe", body, 200, datetime(2026, 8, 25))


def test_valid_feed_parses_and_skips_the_legal_element():
    result = _Probe().parse(_raw(FEED), SELECTORS)
    assert result.status == "ok"
    assert len(result.postings) == 2
    assert result.postings[0].external_id == "1001"
    assert result.postings[0].salary_raw == "$70,000 - $90,000"
    assert result.postings[1].salary_raw is None


def test_feed_with_only_the_legal_element_is_empty_not_broken():
    assert _Probe().parse(_raw(EMPTY), SELECTORS).status == "empty"


def test_non_json_body_is_broken():
    result = _Probe().parse(_raw(NOT_JSON), SELECTORS)
    assert result.status == "broken"
    assert result.diagnostics["reason"] == "invalid json"


def test_json_of_the_wrong_shape_is_broken():
    result = _Probe().parse(_raw(WRONG_SHAPE), SELECTORS)
    assert result.status == "broken"
    assert result.diagnostics["reason"] == "root is not a list"


def test_items_missing_required_fields_are_dropped_and_all_dropped_is_broken():
    body = json.dumps([{"legal": "x"}, {"id": "9", "company": "NoTitle"}]).encode()
    result = _Probe().parse(_raw(body), SELECTORS)
    assert result.status == "broken"
    assert result.diagnostics["dropped"] == 1


def _blank_posting() -> RawPosting:
    return RawPosting(external_id="1", url="https://x", title="t", company="c", description="")


def test_remoteok_forces_remote_arrangement_regardless_of_item_content():
    posting = RemoteOkAdapter().post_process(_blank_posting(), {})
    assert posting.arrangement_hint == "remote"


def test_remoteok_zero_salary_means_not_stated_not_dollar_zero():
    # RemoteOK uses 0 for "not stated" — a "$0" string would parse to a real
    # salary of zero and could wrongly fail the owner's salary filter.
    posting = RemoteOkAdapter().post_process(_blank_posting(), {"salary_min": 0, "salary_max": 0})
    assert posting.salary_raw is None


def test_remoteok_absent_salary_fields_produce_no_salary_raw():
    posting = RemoteOkAdapter().post_process(_blank_posting(), {})
    assert posting.salary_raw is None


def test_remoteok_synthesizes_a_range_salary_raw_that_round_trips():
    posting = RemoteOkAdapter().post_process(_blank_posting(), {"salary_min": 90000, "salary_max": 110000})
    assert posting.salary_raw == "$90000 - $110000"
    parsed = salary.parse(posting.salary_raw)
    assert (parsed.min, parsed.max, parsed.currency) == (90000, 110000, "USD")


def test_remoteok_synthesizes_a_single_figure_when_min_equals_max():
    posting = RemoteOkAdapter().post_process(_blank_posting(), {"salary_min": 90000, "salary_max": 90000})
    assert posting.salary_raw == "$90000"
    parsed = salary.parse(posting.salary_raw)
    assert (parsed.min, parsed.max, parsed.currency) == (90000, 90000, "USD")


def test_remoteok_synthesizes_from_a_lone_bound_when_only_one_side_is_stated():
    posting = RemoteOkAdapter().post_process(_blank_posting(), {"salary_min": 90000, "salary_max": 0})
    assert posting.salary_raw == "$90000"
    assert salary.parse(posting.salary_raw).min == 90000


REMOTEOK = pathlib.Path(__file__).parent / "fixtures" / "remoteok"


def _remoteok_fixture(name: str) -> RawFetch:
    body = gzip.decompress((REMOTEOK / name).read_bytes())
    return RawFetch("remoteok", body, 200, datetime(2026, 8, 25))


def test_remoteok_valid_fixture_parses():
    selectors = json.loads((REMOTEOK / "selectors.json").read_text())
    result = RemoteOkAdapter().parse(_remoteok_fixture("valid.json.gz"), selectors)
    assert result.status == "ok"
    assert len(result.postings) >= 30
    assert all(p.external_id and p.title and p.company and p.url for p in result.postings)
    assert all(p.arrangement_hint == "remote" for p in result.postings)


def test_remoteok_empty_fixture_classifies_empty():
    selectors = json.loads((REMOTEOK / "selectors.json").read_text())
    result = RemoteOkAdapter().parse(_remoteok_fixture("empty.json.gz"), selectors)
    assert result.status == "empty"


def test_wwr_post_process_forces_remote_arrangement():
    posting = WeWorkRemotelyAdapter().post_process(_blank_posting(), {})
    assert posting.arrangement_hint == "remote"


def test_wwr_post_process_splits_company_and_title_on_first_colon():
    posting = _blank_posting()
    posting.title = "Yooli: FULL TIME: Software Engineer Position - React and Rest"
    posting.company = posting.title
    result = WeWorkRemotelyAdapter().post_process(posting, {})
    assert result.company == "Yooli"
    assert result.title == "FULL TIME: Software Engineer Position - React and Rest"


WWR = pathlib.Path(__file__).parent / "fixtures" / "weworkremotely"


def _wwr_fixture(name: str) -> RawFetch:
    body = gzip.decompress((WWR / name).read_bytes())
    return RawFetch("weworkremotely", body, 200, datetime(2026, 8, 25))


def test_wwr_valid_fixture_parses():
    selectors = json.loads((WWR / "selectors.json").read_text())
    result = WeWorkRemotelyAdapter().parse(_wwr_fixture("valid.json.gz"), selectors)
    assert result.status == "ok"
    assert all(p.company and p.title and p.company != p.title for p in result.postings)
    assert all(p.arrangement_hint == "remote" for p in result.postings)
    assert all(p.salary_raw is None for p in result.postings)


def test_wwr_empty_fixture_classifies_empty():
    selectors = json.loads((WWR / "selectors.json").read_text())
    result = WeWorkRemotelyAdapter().parse(_wwr_fixture("empty.json.gz"), selectors)
    assert result.status == "empty"
