import pytest

from jobsearch.profile import compute_hash, validate_weights


def test_hash_ignores_key_order_and_formatting():
    a = {"skills": {"expert": ["php", "vue"]}, "version": 1}
    b = {"version": 1, "skills": {"expert": ["php", "vue"]}}
    assert compute_hash(a) == compute_hash(b)


def test_hash_changes_when_a_value_changes():
    a = {"version": 1, "preferences": {"salary": {"min": 4000}}}
    b = {"version": 1, "preferences": {"salary": {"min": 5000}}}
    assert compute_hash(a) != compute_hash(b)


def test_weights_must_sum_to_100():
    with pytest.raises(ValueError, match="sum to 100"):
        validate_weights({"technical_fit": 30, "seniority_fit": 15})


def test_weights_reject_unknown_dimension():
    weights = {
        "technical_fit": 20, "seniority_fit": 15, "compensation_fit": 15,
        "arrangement_fit": 10, "domain_fit": 10, "company_fit": 10,
        "growth_potential": 10, "vibes": 10,
    }
    with pytest.raises(ValueError, match="vibes"):
        validate_weights(weights)


def test_weights_reject_red_flags_as_a_dimension():
    weights = {
        "technical_fit": 25, "seniority_fit": 15, "compensation_fit": 15,
        "arrangement_fit": 10, "domain_fit": 10, "company_fit": 10,
        "growth_potential": 10, "red_flags": 5,
    }
    with pytest.raises(ValueError, match="red_flags"):
        validate_weights(weights)


def test_weights_reject_a_missing_dimension_even_when_they_sum_to_100():
    weights = {
        "technical_fit": 35, "seniority_fit": 15, "compensation_fit": 15,
        "arrangement_fit": 10, "domain_fit": 15, "company_fit": 10,
        # growth_potential omitted
    }
    with pytest.raises(ValueError, match="missing"):
        validate_weights(weights)
