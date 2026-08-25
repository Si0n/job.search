import pytest

from jobsearch.filters import evaluate

RULES = {
    "require_arrangement": ["remote", "hybrid"],
    "require_employment": ["full-time", "contract"],
    "min_salary_monthly_eur": 4000,
    "exclude_keywords": ["casino", "gambling"],
    "exclude_companies": ["Evil Corp"],
    "languages_required": ["english"],
}


def job(**overrides):
    base = {
        "arrangement": "remote",
        "employment_type": "full-time",
        "salary_monthly_eur": 6000,
        "salary_source": "posting",
        "company": "Acme",
        "text": "senior php developer, english required, laravel and vue",
    }
    base.update(overrides)
    return base


def test_a_matching_job_passes():
    verdict = evaluate(job(), RULES)
    assert verdict.passed is True
    assert verdict.reason is None


def test_absent_salary_passes_the_floor():
    verdict = evaluate(job(salary_monthly_eur=None, salary_source="absent"), RULES)
    assert verdict.passed is True


def test_explicit_salary_below_the_floor_fails():
    verdict = evaluate(job(salary_monthly_eur=2500), RULES)
    assert verdict.passed is False
    assert "salary" in verdict.reason


def test_unknown_arrangement_passes():
    # Absent is not a violation — the same rule that governs salary.
    assert evaluate(job(arrangement="unknown"), RULES).passed is True


def test_onsite_fails_when_remote_or_hybrid_is_required():
    verdict = evaluate(job(arrangement="onsite"), RULES)
    assert verdict.passed is False
    assert "arrangement" in verdict.reason


def test_unknown_employment_passes_but_a_known_mismatch_fails():
    assert evaluate(job(employment_type="unknown"), RULES).passed is True
    assert evaluate(job(employment_type="internship"), RULES).passed is False


def test_excluded_keyword_fails_case_insensitively():
    verdict = evaluate(job(text="backend dev for a CASINO platform"), RULES)
    assert verdict.passed is False
    assert "casino" in verdict.reason


def test_excluded_company_fails_regardless_of_legal_suffix():
    assert evaluate(job(company="Evil Corp Ltd."), RULES).passed is False


def test_languages_required_is_not_a_hard_filter():
    # Absent is not a violation — the same rule that governs every other filter.
    # languages_required can't reliably tell "no requirement stated" apart from
    # "requires a language the owner lacks," so it's left to scoring entirely;
    # a posting that never mentions a required language must still pass.
    assert evaluate(job(text="senior php developer, laravel"), RULES).passed is True
    assert evaluate(job(text=""), RULES).passed is True


def test_empty_rules_pass_everything():
    assert evaluate(job(arrangement="onsite", salary_monthly_eur=1), {}).passed is True


def test_the_first_failing_rule_is_the_reported_reason():
    verdict = evaluate(job(arrangement="onsite", salary_monthly_eur=100), RULES)
    assert verdict.reason.startswith("arrangement")
