import json

import pytest

from jobsearch.dashboard import (
    JOB_FILTERS, build_view, format_salary, language_requirement, match_tech,
    parse_job_filter,
)

JOB = {
    "id": 42, "title": "Senior PHP Developer", "company": "Acme",
    "location": "Remote (EU)", "arrangement": "remote", "employment_type": "full-time",
    "salary_min": 6000, "salary_max": 7000, "salary_currency": "EUR",
    "salary_period": "month", "salary_source": "posting", "salary_monthly_eur": 6500,
    "first_seen_at": "2026-08-25 09:00:00", "canonical_source_id": 1,
    "score": 8, "red_flag_penalty": 0,
    "dimensions": '{"technical_fit": 9, "seniority_fit": 7}',
    "hard_concerns": "[]", "strengths": '["stack match"]', "weaknesses": '["vague comp"]',
    "verdict": "Strong fit.", "status": None,
}

POSTINGS = [
    {"job_id": 42, "source_id": 1, "source_name": "djinni",
     "url": "https://djinni.co/jobs/101", "posted_at": "2026-08-24 12:00:00",
     "description": "Laravel and Vue, fintech."},
    {"job_id": 42, "source_id": 5, "source_name": "linkedin",
     "url": "https://linkedin.com/jobs/view/9", "posted_at": None,
     "description": "Short blurb."},
]


def test_one_card_per_job_with_every_source_listed():
    view = build_view([JOB], POSTINGS)
    assert len(view) == 1
    card = view[0]
    assert card["id"] == 42
    assert [s["name"] for s in card["sources"]] == ["djinni", "linkedin"]
    assert card["sources"][0]["url"] == "https://djinni.co/jobs/101"


def test_description_comes_from_the_canonical_source():
    # canonical_source_id is 1 (djinni), so its description wins over LinkedIn's blurb.
    assert build_view([JOB], POSTINGS)[0]["description"] == "Laravel and Vue, fintech."


def test_description_falls_back_to_the_longest_when_canonical_has_none():
    postings = [
        {**POSTINGS[0], "description": ""},
        {**POSTINGS[1], "description": "A much longer description than the other one."},
    ]
    assert "much longer" in build_view([JOB], postings)[0]["description"]


def test_json_columns_arrive_as_strings_and_are_parsed():
    card = build_view([JOB], POSTINGS)[0]
    assert card["dimensions"] == {"technical_fit": 9, "seniority_fit": 7}
    assert card["strengths"] == ["stack match"]
    assert card["hard_concerns"] == []


def test_json_columns_already_parsed_by_the_driver_pass_through():
    job = {**JOB, "dimensions": {"technical_fit": 9}, "strengths": ["x"], "hard_concerns": []}
    card = build_view([job], POSTINGS)[0]
    assert card["dimensions"] == {"technical_fit": 9}
    assert card["strengths"] == ["x"]


def test_an_unscored_job_renders_without_crashing():
    job = {**JOB, "score": None, "dimensions": None, "hard_concerns": None,
           "strengths": None, "weaknesses": None, "verdict": None}
    card = build_view([job], POSTINGS)[0]
    assert card["score"] is None
    assert card["dimensions"] == {}
    assert card["strengths"] == []
    assert card["verdict"] == ""


def test_untriaged_job_is_flagged_new_and_triaged_is_not():
    assert build_view([JOB], POSTINGS)[0]["is_new"] is True
    assert build_view([{**JOB, "status": "applied"}], POSTINGS)[0]["is_new"] is False


def test_a_job_with_no_postings_is_dropped_rather_than_rendered_broken():
    assert build_view([JOB], []) == []


def test_cards_are_ordered_by_score_descending():
    low = {**JOB, "id": 1, "score": 5}
    high = {**JOB, "id": 2, "score": 9}
    postings = [{**POSTINGS[0], "job_id": 1}, {**POSTINGS[0], "job_id": 2}]
    assert [c["id"] for c in build_view([low, high], postings)] == [2, 1]


@pytest.mark.parametrize("row,expected_text,stated", [
    ({"salary_source": "posting", "salary_min": 6000, "salary_max": 7000,
      "salary_currency": "EUR", "salary_period": "month"}, "€6000–7000/month", True),
    ({"salary_source": "posting", "salary_min": 6000, "salary_max": 6000,
      "salary_currency": "EUR", "salary_period": "month"}, "€6000/month", True),
    ({"salary_source": "posting", "salary_min": None, "salary_max": 8000,
      "salary_currency": "EUR", "salary_period": "month"}, "up to €8000/month", True),
    ({"salary_source": "posting", "salary_min": 5000, "salary_max": None,
      "salary_currency": "USD", "salary_period": "month"}, "from $5000/month", True),
    ({"salary_source": "absent", "salary_min": None, "salary_max": None,
      "salary_currency": None, "salary_period": None}, "not stated", False),
])
def test_salary_formatting(row, expected_text, stated):
    result = format_salary(row)
    assert result["text"] == expected_text
    assert result["stated"] is stated


def test_jobs_sharing_a_fingerprint_are_flagged_as_duplicates():
    # Two rows, same fingerprint, different ids: the same role that failed the
    # cross-source merge gate. The page must be able to say so.
    a = {**JOB, "id": 1, "fingerprint": "abc123"}
    b = {**JOB, "id": 2, "fingerprint": "abc123"}
    postings = [{**POSTINGS[0], "job_id": 1}, {**POSTINGS[0], "job_id": 2}]
    view = build_view([a, b], postings)
    assert all(c["is_duplicate"] for c in view)


def test_a_job_with_a_unique_fingerprint_is_not_flagged():
    a = {**JOB, "id": 1, "fingerprint": "abc123"}
    b = {**JOB, "id": 2, "fingerprint": "different"}
    postings = [{**POSTINGS[0], "job_id": 1}, {**POSTINGS[0], "job_id": 2}]
    view = build_view([a, b], postings)
    assert not any(c["is_duplicate"] for c in view)


def test_a_missing_fingerprint_never_flags_a_duplicate():
    # Defensive: a NULL fingerprint must not make every such job "duplicate".
    a = {**JOB, "id": 1, "fingerprint": None}
    b = {**JOB, "id": 2, "fingerprint": None}
    postings = [{**POSTINGS[0], "job_id": 1}, {**POSTINGS[0], "job_id": 2}]
    view = build_view([a, b], postings)
    assert not any(c["is_duplicate"] for c in view)


# --- technology matching against the owner's skill tiers ---

SKILLS = {
    "expert": ["PHP", "Laravel", "MySQL", "REST API design"],
    "strong": ["Vue 3", "TypeScript", "Redis", "RabbitMQ"],
    "familiar": ["Python (Flask)", "Node.js"],
}


def test_tech_is_tagged_with_the_tier_it_sits_in():
    found = {t["name"]: t["tier"] for t in match_tech("We use PHP, Laravel, Redis and Node.js", SKILLS)}
    assert found["PHP"] == "expert"
    assert found["Laravel"] == "expert"
    assert found["Redis"] == "strong"
    assert found["Node.js"] == "familiar"


def test_tech_the_job_wants_that_the_profile_does_not_list_is_tier_none():
    found = {t["name"]: t["tier"] for t in match_tech("Kubernetes and Kafka experience required", SKILLS)}
    assert found["Kubernetes"] is None
    assert found["Kafka"] is None


def test_a_technology_is_reported_once_however_often_it_appears():
    names = [t["name"] for t in match_tech("PHP, php, and more PHP", SKILLS)]
    assert names.count("PHP") == 1


def test_aliases_resolve_to_one_canonical_name():
    assert {t["name"] for t in match_tech("Postgres and NodeJS and k8s", SKILLS)} == {
        "PostgreSQL", "Node.js", "Kubernetes"}


def test_bare_go_as_an_english_verb_is_not_matched_as_a_language():
    assert "Go" not in {t["name"] for t in match_tech("You will go to the office and go through code", SKILLS)}
    assert "Go" in {t["name"] for t in match_tech("Backend services written in Go and PHP", SKILLS)}


def test_matches_are_ordered_expert_first_and_unlisted_last():
    tiers = [t["tier"] for t in match_tech("Kubernetes, Redis, PHP, Node.js", SKILLS)]
    assert tiers == ["expert", "strong", "familiar", None]


def test_an_empty_description_yields_nothing():
    assert match_tech("", SKILLS) == []
    assert match_tech(None, SKILLS) == []


# --- language requirement vs the owner's own levels ---

LANGS = ["Ukrainian (native)", "Russian (native)", "English (B1-B2)", "Polish (B1)"]


def test_a_level_above_the_owners_is_flagged_as_a_gap():
    r = language_requirement({"language_hint": "English - C1"}, LANGS)
    assert r["level"] == "C1" and r["gap"] is True


def test_a_level_at_or_below_the_owners_is_not_a_gap():
    assert language_requirement({"language_hint": "English - B2"}, LANGS)["gap"] is False
    assert language_requirement({"language_hint": "English - B1"}, LANGS)["gap"] is False


def test_a_language_the_owner_speaks_natively_is_never_a_gap():
    assert language_requirement({"language_hint": "Ukrainian - C2"}, LANGS)["gap"] is False


def test_a_language_absent_from_the_profile_is_flagged():
    r = language_requirement({"language_hint": "German - B2"}, LANGS)
    assert r["gap"] is True


def test_no_hint_yields_nothing_rather_than_a_false_all_clear():
    assert language_requirement({}, LANGS) is None
    assert language_requirement({"language_hint": ""}, LANGS) is None


# --- per-dimension detail: what each number actually contributed ---

WEIGHTS = {"technical_fit": 30, "seniority_fit": 15, "compensation_fit": 15,
           "arrangement_fit": 10, "domain_fit": 10, "company_fit": 10,
           "growth_potential": 10}


def _detail(job_extra=None, weights=WEIGHTS):
    job = {**JOB, "dimensions": '{"technical_fit": 9, "company_fit": 6}', **(job_extra or {})}
    view = build_view([job], POSTINGS, {"weights": weights})
    return {d["name"]: d for d in view[0]["dimension_detail"]}


def test_each_dimension_reports_its_weight_and_what_it_contributed():
    d = _detail()
    # 9 x 30 / 100 = 2.7 — three times what the same 9 would be worth at weight 10
    assert d["technical_fit"]["weight"] == 30
    assert d["technical_fit"]["contribution"] == 2.7
    assert d["company_fit"]["contribution"] == 0.6


def test_contribution_is_derived_from_the_weight_total_not_a_hardcoded_100():
    # A profile whose weights sum to 50 must still produce a 0-10 scale.
    d = _detail(weights={"technical_fit": 30, "company_fit": 20})
    assert d["technical_fit"]["contribution"] == 5.4


def test_detail_is_ordered_by_contribution_so_the_decisive_dimension_reads_first():
    view = build_view([{**JOB, "dimensions": '{"company_fit": 10, "technical_fit": 7}'}],
                      POSTINGS, {"weights": WEIGHTS})
    names = [d["name"] for d in view[0]["dimension_detail"]]
    assert names[0] == "technical_fit"  # 7x30=2.1 beats 10x10=1.0


def test_a_captured_note_is_carried_through_when_present():
    d = _detail({"dimension_notes": '{"company_fit": "Agency, not a product company"}'})
    assert d["company_fit"]["note"] == "Agency, not a product company"
    assert d["technical_fit"]["note"] is None


def test_a_dimension_with_no_weight_contributes_nothing_rather_than_crashing():
    d = _detail(weights={"technical_fit": 100})
    assert d["company_fit"]["weight"] == 0
    assert d["company_fit"]["contribution"] == 0.0


def test_no_profile_means_no_detail_rather_than_wrong_detail():
    view = build_view([{**JOB, "dimensions": '{"technical_fit": 9}'}], POSTINGS)
    assert view[0]["dimension_detail"] == []


def test_a_draft_written_under_the_current_profile_is_not_stale():
    # The comparison must be against the LIVE profile, not against the job's
    # score — a job can carry an out-of-date score while its draft is current.
    from jobsearch.profile import compute_hash
    prof = {"weights": WEIGHTS, "skills": {}}
    job = {**JOB, "cover_letter": "Hi", "draft_email": "Hi", "why_fit": "-",
           "draft_hash": compute_hash(prof)}
    assert build_view([job], POSTINGS, prof)[0]["draft"]["stale"] is False


def test_a_draft_written_under_an_older_profile_is_stale():
    prof = {"weights": WEIGHTS, "skills": {}}
    job = {**JOB, "cover_letter": "Hi", "draft_email": "Hi", "why_fit": "-",
           "draft_hash": "written-under-something-else"}
    assert build_view([job], POSTINGS, prof)[0]["draft"]["stale"] is True


def test_every_offered_filter_resolves_to_itself():
    for name in JOB_FILTERS:
        assert parse_job_filter(name) == name


@pytest.mark.parametrize("raw", [
    None, "", "triaged", "APPLIED", "all statuses",
    "applied' OR 1=1 --", "replied",
])
def test_an_unknown_filter_falls_back_to_the_working_queue(raw):
    # Never raises and never reaches the SQL: an unusable filter costs the owner
    # the preference, not the page.
    assert parse_job_filter(raw) == "untriaged"
