import json

import pytest

from jobsearch.dashboard import build_view, format_salary

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
