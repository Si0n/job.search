import gzip
import json
import pathlib
from datetime import datetime

import httpx

from jobsearch import normalize, salary
from jobsearch.adapters.ats import AtsAdapter
from jobsearch.adapters.base import JsonAdapter, parse_posted_at
from jobsearch.adapters.euremotejobs import EuRemoteJobsAdapter
from jobsearch.adapters.hnwhoishiring import HackerNewsWhoIsHiringAdapter
from jobsearch.adapters.jobicy import JobicyAdapter
from jobsearch.adapters.jobspresso import JobspressoAdapter
from jobsearch.adapters.landingjobs import LandingJobsAdapter
from jobsearch.adapters.larajobs import LaraJobsAdapter
from jobsearch.adapters.remoteok import RemoteOkAdapter
from jobsearch.adapters.nofluffjobs import NoFluffJobsAdapter
from jobsearch.adapters.remotive import RemotiveAdapter
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


def test_a_javascript_url_is_dropped_and_counted():
    body = json.dumps([
        {"legal": "notice"},
        {"id": "2001", "url": "javascript:alert(1)", "position": "Evil Job", "company": "Evil Corp"},
        {"id": "2002", "url": "https://remoteok.com/l/2002", "position": "Good Job", "company": "Acme"},
    ]).encode()
    result = _Probe().parse(_raw(body), SELECTORS)
    assert result.status == "ok"
    assert len(result.postings) == 1
    assert result.postings[0].external_id == "2002"
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


def test_a_title_without_the_company_separator_leaves_company_blank_not_wrong():
    adapter = WeWorkRemotelyAdapter()
    posting = RawPosting("1", "u", "Staff Software Engineer", "Staff Software Engineer", "d")
    result = adapter.post_process(posting, {})
    assert result.company == ""
    assert result.title == "Staff Software Engineer"


def test_commas_on_both_sides_of_the_separator_survive_the_split():
    adapter = WeWorkRemotelyAdapter()
    fused = "Gusto, Inc.: Staff Software Engineer, Time and Scheduling"
    posting = RawPosting("1", "u", fused, fused, "d")
    result = adapter.post_process(posting, {})
    assert result.company == "Gusto, Inc."
    assert result.title == "Staff Software Engineer, Time and Scheduling"


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


# --- RSS sources share one conversion; LaraJobs namespaces its own fields ---

def test_rss_conversion_strips_xml_namespaces_so_prefixed_fields_are_reachable():
    # LaraJobs puts salary/location/job_type in a job: namespace. Selector JSON
    # names plain keys, so the conversion must flatten the prefix away.
    from jobsearch.adapters.base import rss_to_items
    xml = b"""<?xml version="1.0"?><rss version="2.0" xmlns:job="http://larajobs.com/ns">
      <channel><item>
        <title>Senior Laravel Engineer</title><link>https://larajobs.com/job/1</link>
        <guid>https://larajobs.com/job/1</guid>
        <job:salary><![CDATA[$170,000-$220,000]]></job:salary>
        <job:company><![CDATA[Acme]]></job:company>
        <job:location><![CDATA[Remote/USA]]></job:location>
      </item></channel></rss>"""
    items = rss_to_items(xml.decode())
    assert len(items) == 1
    assert items[0]["salary"] == "$170,000-$220,000"
    assert items[0]["company"] == "Acme"
    assert items[0]["location"] == "Remote/USA"


def test_rss_conversion_falls_back_to_link_when_guid_is_absent():
    from jobsearch.adapters.base import rss_to_items
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><item>
      <title>T</title><link>https://x.test/1</link></item></channel></rss>"""
    assert rss_to_items(xml)[0]["guid"] == "https://x.test/1"


LARAJOBS = pathlib.Path(__file__).parent / "fixtures" / "larajobs"


def _larajobs_fixture(name: str) -> RawFetch:
    body = gzip.decompress((LARAJOBS / name).read_bytes())
    return RawFetch("larajobs", body, 200, datetime(2026, 8, 25))


def test_larajobs_valid_fixture_parses():
    selectors = json.loads((LARAJOBS / "selectors.json").read_text())
    result = LaraJobsAdapter().parse(_larajobs_fixture("valid.json.gz"), selectors)
    assert result.status == "ok"
    assert all(p.external_id and p.title and p.company and p.url for p in result.postings)


def test_larajobs_reads_salary_from_its_namespaced_feed_field():
    # The reason this source was added: it states a figure on roughly half its
    # listings, against about 4% across the four original sources. Those figures
    # arrive as `job:salary`, so a regression in namespace stripping would show
    # up here as a silently salary-free board rather than as a parse failure.
    selectors = json.loads((LARAJOBS / "selectors.json").read_text())
    result = LaraJobsAdapter().parse(_larajobs_fixture("valid.json.gz"), selectors)
    assert sum(1 for p in result.postings if p.salary_raw) >= 3


def test_larajobs_empty_fixture_classifies_empty():
    selectors = json.loads((LARAJOBS / "selectors.json").read_text())
    result = LaraJobsAdapter().parse(_larajobs_fixture("empty.json.gz"), selectors)
    assert result.status == "empty"


REMOTIVE = pathlib.Path(__file__).parent / "fixtures" / "remotive"


def _remotive_fixture(name: str) -> RawFetch:
    body = gzip.decompress((REMOTIVE / name).read_bytes())
    return RawFetch("remotive", body, 200, datetime(2026, 8, 25))


def test_remotive_valid_fixture_parses():
    selectors = json.loads((REMOTIVE / "selectors.json").read_text())
    result = RemotiveAdapter().parse(_remotive_fixture("valid.json.gz"), selectors)
    assert result.status == "ok"
    assert all(p.external_id and p.title and p.company and p.url for p in result.postings)


def test_remotive_forces_remote_arrangement_regardless_of_item_content():
    selectors = json.loads((REMOTIVE / "selectors.json").read_text())
    result = RemotiveAdapter().parse(_remotive_fixture("valid.json.gz"), selectors)
    assert all(p.arrangement_hint == "Remote" for p in result.postings)


def test_remotive_empty_fixture_classifies_empty():
    selectors = json.loads((REMOTIVE / "selectors.json").read_text())
    result = RemotiveAdapter().parse(_remotive_fixture("empty.json.gz"), selectors)
    assert result.status == "empty"


def test_larajobs_describes_a_role_from_feed_fields_when_the_feed_body_is_blank():
    # LaraJobs publishes an empty CDATA description and points `link` at the
    # employer's own site, so there is no posting body to fetch. Without this,
    # every LaraJobs job reaches pass-2 scoring with an empty description and
    # gets judged on its title alone, while the feed's `tags` — the actual
    # stack, and the single most useful field for technical_fit — is discarded.
    selectors = json.loads((LARAJOBS / "selectors.json").read_text())
    result = LaraJobsAdapter().parse(_larajobs_fixture("valid.json.gz"), selectors)
    assert all(p.description for p in result.postings)
    tagged = [p for p in result.postings if "Laravel" in (p.description or "")]
    assert tagged, "the stack from `tags` must reach the description"
    assert "employer" in tagged[0].description
    assert "_" not in tagged[0].description.split("Type: ")[-1].split(".")[0]


def test_larajobs_keeps_a_real_feed_body_when_one_is_published():
    selectors = json.loads((LARAJOBS / "selectors.json").read_text())
    fetch = _larajobs_fixture("valid.json.gz")
    items = json.loads(fetch.body)
    items[0]["description"] = "A real posting body, published in the feed."
    fetch = RawFetch("larajobs", json.dumps(items).encode(), 200, datetime(2026, 8, 25))
    result = LaraJobsAdapter().parse(fetch, selectors)
    assert result.postings[0].description == "A real posting body, published in the feed."


JOBICY = pathlib.Path(__file__).parent / "fixtures" / "jobicy"


def _jobicy_fixture(name: str) -> RawFetch:
    body = gzip.decompress((JOBICY / name).read_bytes())
    return RawFetch("jobicy", body, 200, datetime(2026, 8, 25))


def test_jobicy_valid_fixture_parses():
    selectors = json.loads((JOBICY / "selectors.json").read_text())
    result = JobicyAdapter().parse(_jobicy_fixture("valid.json.gz"), selectors)
    assert result.status == "ok"
    assert all(p.external_id and p.title and p.company and p.url for p in result.postings)
    assert all(p.arrangement_hint == "remote" for p in result.postings)


def test_jobicy_empty_fixture_classifies_empty():
    selectors = json.loads((JOBICY / "selectors.json").read_text())
    result = JobicyAdapter().parse(_jobicy_fixture("empty.json.gz"), selectors)
    assert result.status == "empty"


def test_jobicy_salaries_survive_into_a_parseable_string():
    # This board was added for its salary coverage, so a regression that leaves
    # the figures unparseable removes the only reason to poll it — while every
    # posting still arrives and the source still reports "ok".
    from jobsearch.salary import parse as parse_salary
    selectors = json.loads((JOBICY / "selectors.json").read_text())
    result = JobicyAdapter().parse(_jobicy_fixture("valid.json.gz"), selectors)
    stated = [p for p in result.postings if p.salary_raw]
    assert len(stated) >= 10
    assert all(parse_salary(p.salary_raw).min for p in stated)


def test_jobicy_keeps_a_monthly_figure_monthly():
    selectors = json.loads((JOBICY / "selectors.json").read_text())
    fetch = _jobicy_fixture("valid.json.gz")
    payload = json.loads(fetch.body)
    payload["jobs"][0].update(salaryMin=9000, salaryMax=9000,
                              salaryCurrency="EUR", salaryPeriod="monthly")
    fetch = RawFetch("jobicy", json.dumps(payload).encode(), 200, datetime(2026, 8, 25))
    result = JobicyAdapter().parse(fetch, selectors)
    from jobsearch.salary import parse as parse_salary
    assert parse_salary(result.postings[0].salary_raw).period == "month"


NOFLUFF = pathlib.Path(__file__).parent / "fixtures" / "nofluffjobs"


def _nofluff_fixture(name: str) -> RawFetch:
    body = gzip.decompress((NOFLUFF / name).read_bytes())
    return RawFetch("nofluffjobs", body, 200, datetime(2026, 8, 25))


def test_nofluffjobs_valid_fixture_parses():
    selectors = json.loads((NOFLUFF / "selectors.json").read_text())
    result = NoFluffJobsAdapter().parse(_nofluff_fixture("valid.json.gz"), selectors)
    assert result.status == "ok"
    assert result.diagnostics["dropped"] == 0
    assert all(p.url.startswith("https://") for p in result.postings)


def test_nofluffjobs_empty_fixture_classifies_empty():
    selectors = json.loads((NOFLUFF / "selectors.json").read_text())
    result = NoFluffJobsAdapter().parse(_nofluff_fixture("empty.json.gz"), selectors)
    assert result.status == "empty"


def test_nofluffjobs_monthly_zloty_salaries_survive_intact():
    # Disclosure is mandatory in Poland, so this source states a figure on
    # every posting — the reason it is polled at all. The figures arrive as
    # JSON floats in PLN per month, where a rendering slip both inflates the
    # amount tenfold and flips the period to annual.
    from jobsearch.salary import parse as parse_salary
    selectors = json.loads((NOFLUFF / "selectors.json").read_text())
    result = NoFluffJobsAdapter().parse(_nofluff_fixture("valid.json.gz"), selectors)
    stated = [p for p in result.postings if p.salary_raw]
    assert len(stated) > len(result.postings) * 0.9
    parsed = [parse_salary(p.salary_raw) for p in stated]
    assert all(s.currency == "PLN" and s.period == "month" for s in parsed)
    assert all(1000 < s.min < 200000 for s in parsed)


def test_nofluffjobs_absolutizes_slugs_before_the_scheme_check():
    # The API returns `url` as a bare slug. parse() drops any posting whose URL
    # has no http(s) scheme, so if fetch stopped absolutizing, every posting
    # would be dropped and the source would report "broken" with 775 rejects.
    adapter = NoFluffJobsAdapter()
    selectors = json.loads((NOFLUFF / "selectors.json").read_text())
    result = adapter.parse(_nofluff_fixture("valid.json.gz"), selectors)
    assert result.postings
    assert all(p.url.startswith(adapter.base_url + "/job/") for p in result.postings)


def test_a_304_is_returned_rather_than_raised(monkeypatch):
    # httpx treats 304 as a redirect, so raise_for_status() rejects it. That
    # turned every unchanged source into an "error" run, and the not-modified
    # handling downstream — which keeps an unchanged source from being aged out
    # as if its postings had disappeared — was unreachable.
    import httpx

    from jobsearch.adapters.base import Adapter

    def fake_request(method, url, **kwargs):
        return httpx.Response(304, headers={"ETag": "abc"}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake_request)
    raw = RemoteOkAdapter()._get("https://remoteok.com/api", {"etag": "abc"})
    assert raw.http_status == 304
    assert raw.etag == "abc"


def test_nofluffjobs_collapses_one_job_repeated_per_voivodeship():
    # This board lists a job once per Polish region: a single search returned
    # 174 rows that were 13 postings, one of them repeated 34 times. Each copy
    # has its own id and URL, and same-source postings are deliberately never
    # merged, so uncollapsed they fill the scoring queue with one role.
    selectors = json.loads((NOFLUFF / "selectors.json").read_text())
    result = NoFluffJobsAdapter().parse(_nofluff_fixture("valid.json.gz"), selectors)
    ids = [p.external_id for p in result.postings]
    assert len(ids) == len(set(ids))
    titles = [(p.company, p.title) for p in result.postings]
    assert len(titles) == len(set(titles))


def test_nofluffjobs_postings_carry_a_description():
    # The search response has no description field at all. Without the detail
    # fetch every posting reaches pass-2 scoring with an empty body.
    selectors = json.loads((NOFLUFF / "selectors.json").read_text())
    result = NoFluffJobsAdapter().parse(_nofluff_fixture("valid.json.gz"), selectors)
    assert all(p.description for p in result.postings)
    assert any("Must have" in p.description for p in result.postings)


# --- Keyword-searchable remote boards (euremotejobs, jobspresso, landing.jobs) ---

def _fixture(source: str, name: str) -> RawFetch:
    path = pathlib.Path(__file__).parent / "fixtures" / source / name
    return RawFetch(source, gzip.decompress(path.read_bytes()), 200, datetime(2026, 8, 27))


def _selectors(source: str) -> dict:
    path = pathlib.Path(__file__).parent / "fixtures" / source / "selectors.json"
    return json.loads(path.read_text())


def test_wp_job_feed_url_carries_the_keyword_that_makes_these_boards_worth_harvesting():
    url = EuRemoteJobsAdapter().build_url({"search_keywords": "php"})
    assert url == "https://euremotejobs.com/?feed=job_feed&search_keywords=php"


def test_wp_job_feed_url_omits_parameters_the_query_does_not_set():
    assert EuRemoteJobsAdapter().build_url({}) == "https://euremotejobs.com/?feed=job_feed"


def test_euremotejobs_valid_fixture_parses_with_company_and_location():
    result = EuRemoteJobsAdapter().parse(_fixture("euremotejobs", "valid.json.gz"),
                                         _selectors("euremotejobs"))
    assert result.status == "ok"
    assert len(result.postings) == 10
    assert all(p.external_id and p.url and p.title and p.company for p in result.postings)
    assert all(p.arrangement_hint == "remote" for p in result.postings)


def test_euremotejobs_carries_a_posting_body_rather_than_a_stub():
    result = EuRemoteJobsAdapter().parse(_fixture("euremotejobs", "valid.json.gz"),
                                         _selectors("euremotejobs"))
    # The reason this source outranks the aggregators on priority: LinkedIn and
    # the boards that also list these roles hand over a title and little else.
    assert all(len(p.description) > 200 for p in result.postings)


def test_jobspresso_does_not_map_its_unreliable_job_type_to_employment_hint():
    result = JobspressoAdapter().parse(_fixture("jobspresso", "valid.json.gz"),
                                       _selectors("jobspresso"))
    assert result.status == "ok"
    php = [p for p in result.postings if "PHP" in p.title]
    assert php, "fixture should contain the keyword-matched PHP roles"
    # The board types this backend role "Marketing". Mapping it would hand the
    # employment filter a claim the posting never made.
    assert all(p.employment_hint is None for p in result.postings)
    assert any(p.meta.get("job_type") == "Marketing" for p in result.postings)


def test_both_wp_boards_classify_an_empty_feed_as_empty_not_broken():
    for source, adapter in (("euremotejobs", EuRemoteJobsAdapter()),
                            ("jobspresso", JobspressoAdapter())):
        result = adapter.parse(_fixture(source, "empty.json.gz"), _selectors(source))
        assert result.status == "empty", source


def test_landingjobs_recovers_the_company_from_the_posting_url():
    item = {"url": "https://landing.jobs/at/touchpoints-health/founding-engineer"}
    LandingJobsAdapter._enrich(item)
    assert item["company"] == "Touchpoints Health"


def test_landingjobs_leaves_company_absent_when_the_url_shape_changes():
    # Dropped by parse() with a diagnostic, rather than invented — a placeholder
    # would later read as a real employer.
    item = {"url": "https://landing.jobs/jobs/19622"}
    LandingJobsAdapter._enrich(item)
    assert "company" not in item


def test_landingjobs_composes_a_salary_string_the_parser_understands():
    item = {"gross_salary_low": 54000, "gross_salary_high": 69000, "currency_code": "EUR"}
    LandingJobsAdapter._enrich(item)
    assert item["salary_raw"] == "54000 - 69000 EUR/year"
    parsed = salary.parse(item["salary_raw"])
    assert (parsed.min, parsed.max, parsed.currency, parsed.period) == (54000, 69000, "EUR", "year")


def test_landingjobs_handles_a_one_sided_salary_band():
    item = {"gross_salary_low": 45000, "gross_salary_high": None, "currency_code": "EUR"}
    LandingJobsAdapter._enrich(item)
    assert item["salary_raw"] == "45000 EUR/year"


def test_landingjobs_states_no_salary_when_the_api_gives_no_figures():
    item = {"url": "https://landing.jobs/at/acme/dev"}
    LandingJobsAdapter._enrich(item)
    assert "salary_raw" not in item


def test_landingjobs_marks_remote_postings_and_leaves_the_rest_unclaimed():
    remote = {"remote": True}
    onsite = {"remote": False}
    LandingJobsAdapter._enrich(remote)
    LandingJobsAdapter._enrich(onsite)
    assert remote["arrangement_hint"] == "remote"
    assert "arrangement_hint" not in onsite


def test_landingjobs_builds_a_location_from_city_falling_back_to_country():
    item = {"locations": [{"city": "Lisbon"}, {"country_code": "PT"}, {"city": "Lisbon"}]}
    LandingJobsAdapter._enrich(item)
    assert item["location"] == "Lisbon, PT"


def test_landingjobs_enriches_the_raw_api_body_during_fetch(monkeypatch):
    adapter = LandingJobsAdapter()
    raw = _fixture("landingjobs", "valid.json.gz")
    monkeypatch.setattr(adapter, "_get", lambda url, conditional: raw)
    result = adapter.parse(adapter.fetch({"q": "php"}), _selectors("landingjobs"))
    assert result.status == "ok"
    assert all(p.company and p.description for p in result.postings)
    assert any(p.salary_raw and salary.parse(p.salary_raw).min for p in result.postings)


def test_landingjobs_leaves_a_non_json_body_for_parse_to_classify(monkeypatch):
    adapter = LandingJobsAdapter()
    raw = RawFetch("landingjobs", NOT_JSON, 200, datetime(2026, 8, 27))
    monkeypatch.setattr(adapter, "_get", lambda url, conditional: raw)
    assert adapter.fetch({}).body == NOT_JSON
    assert adapter.parse(adapter.fetch({}), _selectors("landingjobs")).status == "broken"


def test_every_registered_adapter_is_reachable_by_its_own_name():
    from jobsearch.adapters import registry
    for name, cls in registry.ADAPTERS.items():
        assert registry.get(name).name == name == cls.name


# --- HN "Who is hiring?": comments written to a convention, not a schema ---

HN = pathlib.Path(__file__).parent / "fixtures" / "hnwhoishiring"


def _hn(name: str) -> str:
    return gzip.decompress((HN / name).read_bytes()).decode()


def _comment(text: str, cid: int = 111) -> dict:
    return {"id": cid, "author": "someone", "text": text}


def test_hn_picks_the_hiring_thread_not_the_wants_to_be_hired_one():
    # The same account posts both on the same day; only one is job adverts.
    assert HackerNewsWhoIsHiringAdapter._latest_thread(_hn("search.json.gz")) == "49156683"


def test_hn_returns_no_thread_rather_than_raising_when_the_search_is_empty():
    assert HackerNewsWhoIsHiringAdapter._latest_thread('{"hits": []}') is None
    assert HackerNewsWhoIsHiringAdapter._latest_thread("not json") is None


def test_hn_parses_the_conventional_header_into_company_and_role():
    record = HackerNewsWhoIsHiringAdapter._as_record(
        _comment("Acme Corp | Senior PHP Engineer | Berlin | REMOTE | PHP, Laravel"),
        "Acme Corp | Senior PHP Engineer | Berlin | REMOTE | PHP, Laravel")
    assert record["company"] == "Acme Corp"
    assert record["title"] == "Senior PHP Engineer"
    assert record["arrangement_hint"] == "remote"
    assert record["external_id"] == "hn-111"
    assert record["url"] == "https://news.ycombinator.com/item?id=111"


def test_hn_skips_prose_that_is_not_a_job_advert():
    # Threads collect asides and questions. Without pipes there is no header to
    # read, and inventing a title would file an opinion as a vacancy.
    body = "Does anyone else find these threads useless for European candidates?"
    assert HackerNewsWhoIsHiringAdapter._as_record(_comment(body), body) is None


def test_hn_drops_a_url_only_segment_rather_than_calling_it_a_job_title():
    body = "Seeq | https://seeq.com | Staff Engineer | REMOTE"
    record = HackerNewsWhoIsHiringAdapter._as_record(_comment(body), body)
    assert record["company"] == "Seeq"
    assert record["title"] == "Staff Engineer"


def test_hn_strips_a_url_appended_to_the_company_name():
    body = "Snout https://snout.com/ | Multiple Engineering Roles | REMOTE"
    record = HackerNewsWhoIsHiringAdapter._as_record(_comment(body), body)
    assert record["company"] == "Snout"


def test_hn_does_not_claim_remote_when_the_header_rules_it_out():
    body = "Acme | Backend Engineer | New York | ONSITE ONLY | Go"
    record = HackerNewsWhoIsHiringAdapter._as_record(_comment(body), body)
    assert "arrangement_hint" not in record


def test_hn_keyword_filter_targets_the_domain_and_keeps_the_volume_sane():
    payload = _hn("thread.json.gz")
    everything = HackerNewsWhoIsHiringAdapter._postings(payload, [])
    targeted = HackerNewsWhoIsHiringAdapter._postings(
        payload, ["PHP", "Laravel", "Symfony", "payments", "fintech"])
    assert everything, "fixture should parse some postings"
    assert 0 < len(targeted) < len(everything)


def test_hn_postings_all_carry_the_fields_the_pipeline_requires():
    postings = HackerNewsWhoIsHiringAdapter._postings(_hn("thread.json.gz"), [])
    assert all(p["external_id"] and p["url"] and p["title"] and p["company"] for p in postings)
    assert all(p["description"] for p in postings)


def test_hn_html_comment_body_becomes_readable_text():
    from jobsearch.adapters.hnwhoishiring import _plain
    raw = "Acme | Dev | REMOTE<p>We use PHP &amp; MySQL.<p>Email <a href=\"x\">jobs@acme.com</a>"
    text = _plain(raw)
    assert "PHP & MySQL" in text
    assert "jobs@acme.com" in text
    assert "<p>" not in text and "<a" not in text


def test_hn_fixture_parses_through_the_full_adapter(monkeypatch):
    adapter = HackerNewsWhoIsHiringAdapter()
    responses = [RawFetch("hnwhoishiring", _hn("search.json.gz").encode(), 200, datetime(2026, 8, 27)),
                 RawFetch("hnwhoishiring", _hn("thread.json.gz").encode(), 200, datetime(2026, 8, 27))]
    monkeypatch.setattr(adapter, "_get", lambda url, conditional: responses.pop(0))
    result = adapter.parse(adapter.fetch({"keywords": ["payments", "fintech", "PHP"]}),
                           json.loads((HN / "selectors.json").read_text()))
    assert result.status == "ok"
    assert all(p.company and p.title and p.description for p in result.postings)


def test_hn_reports_empty_rather_than_broken_before_the_month_thread_exists(monkeypatch):
    adapter = HackerNewsWhoIsHiringAdapter()
    empty = RawFetch("hnwhoishiring", b'{"hits": []}', 200, datetime(2026, 8, 27))
    monkeypatch.setattr(adapter, "_get", lambda url, conditional: empty)
    result = adapter.parse(adapter.fetch({}), json.loads((HN / "selectors.json").read_text()))
    assert result.status == "empty"


# --- Company ATS boards: many employers behind one source ---

ATS = pathlib.Path(__file__).parent / "fixtures" / "ats"

ATS_QUERY = {
    "greenhouse": {"gocardless": "GoCardless"},
    "ashby": {"unit": "Unit"},
    "lever": {"finix": "Finix"},
    "keywords": ["PHP", "Laravel", "Symfony"],
    "fallback_keywords": ["backend", "software engineer",
                          "software development engineer", "principal engineer"],
    "exclude_title_keywords": ["frontend", "support", "sales", "manager"],
}


def _ats(name: str) -> str:
    return gzip.decompress((ATS / name).read_bytes()).decode()


def _ats_records(vendor: str, slug: str, company: str, fixture: str) -> list[dict]:
    return AtsAdapter._records(vendor, slug, company, _ats(fixture))


def test_ats_builds_the_endpoint_for_each_vendor():
    build = AtsAdapter().build_url
    assert build({"vendor": "greenhouse", "slug": "monzo"}) == (
        "https://boards-api.greenhouse.io/v1/boards/monzo/jobs?content=true")
    assert build({"vendor": "ashby", "slug": "unit"}) == (
        "https://api.ashbyhq.com/posting-api/job-board/unit")
    assert build({"vendor": "lever", "slug": "finix"}) == (
        "https://api.lever.co/v0/postings/finix?mode=json")


def test_ats_every_vendor_yields_the_fields_the_pipeline_requires():
    for vendor, slug, company, fixture in (
        ("greenhouse", "gocardless", "GoCardless", "greenhouse.json.gz"),
        ("ashby", "unit", "Unit", "ashby.json.gz"),
        ("lever", "finix", "Finix", "lever.json.gz"),
    ):
        records = _ats_records(vendor, slug, company, fixture)
        assert records, f"{vendor} fixture should yield records"
        for record in records:
            assert record["external_id"] and record["url"]
            assert record["title"] and record["company"]


def test_ats_company_comes_from_config_not_the_vendor_field():
    # Greenhouse publishes a per-job company_name that is a department on some
    # boards ("Wise Worksite Field Sales", "Form3 - External"). The configured
    # display name is the only one that identifies the employer consistently.
    records = _ats_records("greenhouse", "gocardless", "GoCardless Ltd", "greenhouse.json.gz")
    assert {r["company"] for r in records} == {"GoCardless Ltd"}


def test_ats_external_ids_are_namespaced_so_two_boards_cannot_collide():
    records = _ats_records("ashby", "unit", "Unit", "ashby.json.gz")
    assert all(r["external_id"].startswith("ashby-unit-") for r in records)


def test_ats_greenhouse_entity_encoded_content_becomes_readable_html():
    # Greenhouse escapes its HTML a second time: the body arrives as "&lt;div&gt;".
    # Left alone, normalize.description finds no tags to strip and the markup
    # survives into the description as visible text.
    records = _ats_records("greenhouse", "gocardless", "GoCardless", "greenhouse.json.gz")
    body = next(r["description"] for r in records)
    assert "&lt;" not in body
    assert normalize.description(body).strip()


def test_ats_ashby_employment_type_is_translated_for_the_normaliser():
    # Ashby writes "FullTime"; normalize.employment matches "full-time" or
    # "full time" and would read the raw value as unknown.
    records = _ats_records("ashby", "unit", "Unit", "ashby.json.gz")
    hints = {r.get("employment_hint") for r in records}
    assert "full-time" in hints
    assert all(normalize.employment(h, "") == "full-time" for h in hints if h)


def test_ats_lever_millisecond_timestamps_are_not_read_as_the_year_58000():
    # Lever publishes createdAt in milliseconds. Thirteen digits fed to
    # parse_posted_at overflow and the date is lost entirely.
    records = _ats_records("lever", "finix", "Finix", "lever.json.gz")
    dates = [parse_posted_at(r["posted_at"]) for r in records if r.get("posted_at")]
    assert dates, "lever fixture should carry publication dates"
    assert all(2015 < d.year < 2100 for d in dates)


def test_ats_strong_keyword_matches_anywhere_in_the_posting():
    record = {"title": "Senior Engineer", "description": "You will write Laravel."}
    assert AtsAdapter._wanted(record, ["PHP", "Laravel"], [])


def test_ats_fallback_keywords_match_the_title_only():
    # Every engineering job description at a payments company says "backend"
    # somewhere. Matching the body on the fallback list would admit the whole
    # board, which is the failure that took remoteok and weworkremotely offline.
    sales = {"title": "Enterprise Account Executive",
             "description": "Sell our backend payments platform."}
    engineer = {"title": "Senior Backend Engineer", "description": "Payments."}
    assert not AtsAdapter._wanted(sales, ["PHP"], ["backend"])
    assert AtsAdapter._wanted(engineer, ["PHP"], ["backend"])


def test_ats_filter_keeps_engineering_roles_and_drops_the_rest_of_the_board():
    records = _ats_records("lever", "finix", "Finix", "lever.json.gz")
    kept = [r for r in records
            if AtsAdapter._wanted(r, ATS_QUERY["keywords"], ATS_QUERY["fallback_keywords"],
                                  ATS_QUERY["exclude_title_keywords"])]
    titles = {r["title"] for r in kept}
    assert "Senior Software Engineer" in titles
    assert "Enterprise Account Executive" not in titles
    assert "Senior Frontend Engineer" not in titles
    assert 0 < len(kept) < len(records)


def test_ats_excluded_titles_are_vetoed_even_when_the_stack_matches():
    # Mollie's board carries both an "Application Engineer II" and a "Technical
    # Support Specialist", and both name PHP in the body. Only one is the job.
    support = {"title": "Technical Support Specialist",
               "description": "Supporting merchants on our PHP platform."}
    engineer = {"title": "Senior Application Engineer",
                "description": "Our platform is PHP."}
    assert not AtsAdapter._wanted(support, ["PHP"], ["backend"], ["support"])
    assert AtsAdapter._wanted(engineer, ["PHP"], ["backend"], ["support"])


def test_ats_exclusion_beats_a_fallback_title_match():
    role = {"title": "Frontend Backend Platform Manager", "description": ""}
    assert not AtsAdapter._wanted(role, [], ["backend"], ["manager"])


def test_ats_a_posting_without_a_url_is_dropped_rather_than_invented():
    payload = json.dumps({"jobs": [
        {"id": "1", "title": "Senior Backend Engineer", "jobUrl": "https://x/1"},
        {"id": "2", "title": "Senior Backend Engineer"},
    ]})
    records = AtsAdapter._records("ashby", "unit", "Unit", payload)
    assert [r["external_id"] for r in records] == ["ashby-unit-1"]


def test_ats_a_board_that_fails_does_not_take_the_other_boards_with_it(monkeypatch):
    # Sixty-odd boards behind one source: one 404 must not cost the whole run.
    adapter = AtsAdapter()

    def flaky(url, conditional):
        if "gocardless" in url:
            raise httpx.HTTPError("boom")
        return RawFetch("ats", _ats("ashby.json.gz").encode(), 200, datetime(2026, 8, 31))

    monkeypatch.setattr(adapter, "_get", flaky)
    raw = adapter.fetch(ATS_QUERY)
    items = json.loads(raw.text())
    assert items, "the healthy board should still contribute"
    assert {i["company"] for i in items} == {"Unit"}


def test_ats_fixtures_parse_through_the_full_adapter(monkeypatch):
    adapter = AtsAdapter()
    bodies = {"gocardless": "greenhouse.json.gz", "unit": "ashby.json.gz",
              "finix": "lever.json.gz"}

    def serve(url, conditional):
        name = next(f for slug, f in bodies.items() if slug in url)
        return RawFetch("ats", _ats(name).encode(), 200, datetime(2026, 8, 31))

    monkeypatch.setattr(adapter, "_get", serve)
    result = adapter.parse(adapter.fetch(ATS_QUERY),
                           json.loads((ATS / "selectors.json").read_text()))
    assert result.status == "ok"
    assert all(p.company and p.title and p.url for p in result.postings)
    assert {p.company for p in result.postings} <= {"GoCardless", "Unit", "Finix"}


def test_ats_an_empty_configuration_is_empty_not_broken(monkeypatch):
    adapter = AtsAdapter()
    monkeypatch.setattr(adapter, "_get", lambda url, conditional: None)
    result = adapter.parse(adapter.fetch({}),
                           json.loads((ATS / "selectors.json").read_text()))
    assert result.status == "empty"
