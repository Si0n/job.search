import gzip
import json
import pathlib
from datetime import datetime

import pytest

from jobsearch.adapters.base import HtmlAdapter
from jobsearch.adapters.djinni import DjinniAdapter
from jobsearch.adapters.dou import DouAdapter
from jobsearch.models import RawFetch

SELECTORS = {
    "container": "ul.jobs",
    "item": "li.job",
    "empty_state": "div.no-results",
    "minimum_items": 1,
    "fields": {
        "external_id": {"selector": "a.title", "attr": "href", "regex": r"/jobs/(\d+)"},
        "url":         {"selector": "a.title", "attr": "href", "absolute": True},
        "title":       {"selector": "a.title", "attr": "text"},
        "company":     {"selector": ".company", "attr": "text"},
        "location":    {"selector": ".location", "attr": "text"},
        "salary_raw":  {"selector": ".salary", "attr": "text"},
        "description": {"selector": ".desc", "attr": "html"},
    },
}

VALID = b"""<html><body><ul class="jobs">
<li class="job"><a class="title" href="/jobs/101">Senior PHP Developer</a>
<span class="company">Acme</span><span class="location">Remote</span>
<span class="salary">$5000</span><div class="desc"><p>Laravel and Vue.</p></div></li>
<li class="job"><a class="title" href="/jobs/102">Backend Engineer</a>
<span class="company">Globex</span><span class="location">Kyiv</span>
<div class="desc"><p>Go and Postgres.</p></div></li>
</ul></body></html>"""

EMPTY = b"""<html><body><ul class="jobs"></ul>
<div class="no-results">Nothing matches your filters</div></body></html>"""

CHANGED = VALID.replace(b'class="jobs"', b'class="job-list-v2"')

ITEMS_GONE = b"""<html><body><ul class="jobs"></ul></body></html>"""


class _Probe(HtmlAdapter):
    name = "probe"
    base_url = "https://example.test"

    def build_url(self, query):
        return self.base_url


def _raw(body: bytes) -> RawFetch:
    return RawFetch("probe", body, 200, datetime(2026, 8, 25))


def test_valid_markup_parses_ok():
    result = _Probe().parse(_raw(VALID), SELECTORS)
    assert result.status == "ok"
    assert len(result.postings) == 2
    first = result.postings[0]
    assert first.external_id == "101"
    assert first.title == "Senior PHP Developer"
    assert first.company == "Acme"
    assert first.url == "https://example.test/jobs/101"
    assert first.salary_raw == "$5000"
    assert "Laravel" in first.description


def test_missing_optional_field_is_none_not_a_failure():
    result = _Probe().parse(_raw(VALID), SELECTORS)
    assert result.postings[1].salary_raw is None


def test_explicit_empty_state_classifies_empty():
    result = _Probe().parse(_raw(EMPTY), SELECTORS)
    assert result.status == "empty"
    assert result.postings == []


def test_changed_container_markup_classifies_broken():
    result = _Probe().parse(_raw(CHANGED), SELECTORS)
    assert result.status == "broken"
    assert result.diagnostics["container_matched"] == 0


def test_container_present_but_no_items_and_no_empty_marker_is_broken():
    result = _Probe().parse(_raw(ITEMS_GONE), SELECTORS)
    assert result.status == "broken"


def test_items_that_all_fail_required_fields_classify_broken():
    body = VALID.replace(b'class="title"', b'class="headline"')
    result = _Probe().parse(_raw(body), SELECTORS)
    assert result.status == "broken"
    assert result.diagnostics["dropped"] == 2


def test_diagnostics_report_field_fill_rates():
    result = _Probe().parse(_raw(VALID), SELECTORS)
    assert result.diagnostics["fill_rates"]["title"] == 1.0
    assert result.diagnostics["fill_rates"]["salary_raw"] == 0.5


MIN_ITEMS_SELECTORS = {**SELECTORS, "minimum_items": 2}

SINGLE_ITEM = b"""<html><body><ul class="jobs">
<li class="job"><a class="title" href="/jobs/201">Solo PHP Developer</a>
<span class="company">Acme</span><span class="location">Remote</span></li>
</ul></body></html>"""


def test_below_minimum_items_classifies_broken_but_keeps_the_postings():
    # Distinguishes this branch from every other "broken" path: a single item
    # parses successfully (so it isn't dropped-for-required-fields broken),
    # but falls short of minimum_items — the fallback that catches a selector
    # matching an unrelated container.
    result = _Probe().parse(_raw(SINGLE_ITEM), MIN_ITEMS_SELECTORS)
    assert result.status == "broken"
    assert len(result.postings) == 1


# external_id comes from a separate node here (not from the href, as SELECTORS
# does), so a hostile scheme is isolated as the sole reason the first item is
# dropped rather than incidentally failing the external_id regex too.
JAVASCRIPT_URL_SELECTORS = {
    "container": "ul.jobs",
    "item": "li.job",
    "minimum_items": 1,
    "fields": {
        "external_id": {"selector": ".ext-id", "attr": "text"},
        "url":         {"selector": "a.title", "attr": "href", "absolute": True},
        "title":       {"selector": "a.title", "attr": "text"},
        "company":     {"selector": ".company", "attr": "text"},
    },
}

JAVASCRIPT_URL = b"""<html><body><ul class="jobs">
<li class="job"><span class="ext-id">501</span>
<a class="title" href="javascript:alert(document.cookie)">Evil Job</a>
<span class="company">Evil Corp</span></li>
<li class="job"><span class="ext-id">502</span>
<a class="title" href="/jobs/502">Good Job</a>
<span class="company">Acme</span></li>
</ul></body></html>"""


def test_a_javascript_url_is_dropped_and_counted():
    result = _Probe().parse(_raw(JAVASCRIPT_URL), JAVASCRIPT_URL_SELECTORS)
    assert result.status == "ok"
    assert len(result.postings) == 1
    assert result.postings[0].external_id == "502"
    assert result.diagnostics["dropped"] == 1


FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "djinni"
SELECTORS_PATH = FIXTURES / "selectors.json"


def _fixture(name: str) -> RawFetch:
    body = gzip.decompress((FIXTURES / name).read_bytes())
    return RawFetch("djinni", body, 200, datetime(2026, 8, 25))


@pytest.fixture(scope="module")
def djinni_selectors():
    return json.loads(SELECTORS_PATH.read_text())


def test_djinni_valid_fixture_parses(djinni_selectors):
    result = DjinniAdapter().parse(_fixture("valid.html.gz"), djinni_selectors)
    assert result.status == "ok"
    assert len(result.postings) >= 5
    assert all(p.external_id and p.title and p.company and p.url for p in result.postings)


def test_djinni_empty_fixture_classifies_empty(djinni_selectors):
    result = DjinniAdapter().parse(_fixture("empty.html.gz"), djinni_selectors)
    assert result.status == "empty"


def test_djinni_changed_markup_classifies_broken(djinni_selectors):
    result = DjinniAdapter().parse(_fixture("changed-markup.html.gz"), djinni_selectors)
    assert result.status == "broken"


def test_djinni_arrangement_hint_extracted_for_most_postings(djinni_selectors):
    result = DjinniAdapter().parse(_fixture("valid.html.gz"), djinni_selectors)
    non_none = sum(1 for p in result.postings if p.arrangement_hint)
    assert non_none >= 12


DOU_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "dou"


def _dou_fixture(name: str) -> RawFetch:
    body = gzip.decompress((DOU_FIXTURES / name).read_bytes())
    return RawFetch("dou", body, 200, datetime(2026, 8, 25))


@pytest.fixture(scope="module")
def dou_selectors():
    return json.loads((DOU_FIXTURES / "selectors.json").read_text())


def test_dou_valid_fixture_parses(dou_selectors):
    result = DouAdapter().parse(_dou_fixture("valid.html.gz"), dou_selectors)
    assert result.status == "ok"
    assert len(result.postings) >= 5
    assert all(p.external_id and p.title and p.company and p.url for p in result.postings)


def test_dou_empty_fixture_classifies_empty(dou_selectors):
    assert DouAdapter().parse(_dou_fixture("empty.html.gz"), dou_selectors).status == "empty"


def test_dou_changed_markup_classifies_broken(dou_selectors):
    assert DouAdapter().parse(_dou_fixture("changed-markup.html.gz"), dou_selectors).status == "broken"


def test_dou_arrangement_hint_extracted_for_most_postings(dou_selectors):
    result = DouAdapter().parse(_dou_fixture("valid.html.gz"), dou_selectors)
    non_none = sum(1 for p in result.postings if p.arrangement_hint)
    assert non_none >= 17
