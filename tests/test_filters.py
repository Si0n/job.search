from datetime import datetime, timedelta

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


NOW = datetime(2026, 8, 31, 12, 0, 0)
AGE_RULES = {**RULES, "max_age_days": 7}


def test_a_posting_inside_the_age_window_passes():
    verdict = evaluate(job(posted_at=NOW - timedelta(days=3)), AGE_RULES, NOW)
    assert verdict.passed is True


def test_a_posting_older_than_the_window_fails():
    verdict = evaluate(job(posted_at=NOW - timedelta(days=70)), AGE_RULES, NOW)
    assert verdict.passed is False
    assert "70d ago" in verdict.reason
    assert "older than 7d" in verdict.reason


def test_absent_posted_at_passes_the_age_rule():
    """The whole point of the option chosen for this rule: djinni, LinkedIn,
    justjoin.it and HN state no date, and must not be silenced by an age cut."""
    verdict = evaluate(job(posted_at=None), AGE_RULES, NOW)
    assert verdict.passed is True


def test_a_job_with_no_posted_at_key_at_all_passes():
    payload = job()
    payload.pop("posted_at", None)
    assert evaluate(payload, AGE_RULES, NOW).passed is True


def test_the_boundary_day_is_inclusive():
    """Exactly max_age_days old is still inside the window — only strictly
    older fails, so a daily cron does not drop a posting it saw yesterday."""
    assert evaluate(job(posted_at=NOW - timedelta(days=7)), AGE_RULES, NOW).passed is True
    assert evaluate(job(posted_at=NOW - timedelta(days=8)), AGE_RULES, NOW).passed is False


def test_age_rule_absent_from_config_disables_it():
    old = job(posted_at=NOW - timedelta(days=400))
    assert evaluate(old, RULES, NOW).passed is True


def test_age_rule_set_to_zero_disables_it():
    old = job(posted_at=NOW - timedelta(days=400))
    assert evaluate(old, {**RULES, "max_age_days": 0}, NOW).passed is True


def test_a_future_posted_at_is_not_treated_as_stale():
    """A board publishing a timezone-shifted date can land slightly ahead of
    local now; a negative age must not wrap into a failure."""
    verdict = evaluate(job(posted_at=NOW + timedelta(hours=6)), AGE_RULES, NOW)
    assert verdict.passed is True


PART_TIME_RULES = {
    **RULES,
    # The real profile accepts part-time; RULES above does not, and the
    # employment rule runs first, so it would mask the floor behaviour.
    "require_employment": ["full-time", "contract", "part-time"],
    "salary_floor_exempt_employment": ["part-time"],
}


def test_part_time_below_the_floor_passes_when_exempt():
    """A part-time figure is a part-month figure. Judging it against a full
    month's floor rejects the arrangement, not the rate."""
    verdict = evaluate(job(employment_type="part-time", salary_monthly_eur=2500),
                       PART_TIME_RULES)
    assert verdict.passed is True


def test_full_time_below_the_floor_still_fails_when_part_time_is_exempt():
    verdict = evaluate(job(employment_type="full-time", salary_monthly_eur=2500),
                       PART_TIME_RULES)
    assert verdict.passed is False
    assert "below floor" in verdict.reason


def test_contract_below_the_floor_still_fails_when_part_time_is_exempt():
    verdict = evaluate(job(employment_type="contract", salary_monthly_eur=2500),
                       PART_TIME_RULES)
    assert verdict.passed is False


def test_part_time_is_not_exempt_unless_the_rule_names_it():
    """Omitting the key keeps the floor applying to everything, so an existing
    profile that predates this rule behaves exactly as it did before."""
    rules = {**PART_TIME_RULES}
    del rules["salary_floor_exempt_employment"]
    verdict = evaluate(job(employment_type="part-time", salary_monthly_eur=2500), rules)
    assert verdict.passed is False
    assert "below floor" in verdict.reason


def test_the_exemption_does_not_bypass_other_rules():
    """Exempt from the floor is not exempt from everything — a part-time job
    that is on-site or at an excluded company must still fail."""
    onsite = evaluate(job(employment_type="part-time", salary_monthly_eur=2500,
                          arrangement="onsite"), PART_TIME_RULES)
    assert onsite.passed is False
    assert "arrangement" in onsite.reason
