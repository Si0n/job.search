import gzip
import json
import pathlib
from datetime import datetime

from jobsearch import salary
from jobsearch.adapters.base import JsonAdapter
from jobsearch.adapters.euremotejobs import EuRemoteJobsAdapter
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
