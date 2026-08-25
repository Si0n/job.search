from datetime import datetime, timedelta

from jobsearch.dedupe import can_merge, fingerprint, jaccard

NOW = datetime(2026, 8, 25, 9, 0)
LONG = "we are looking for a senior php developer with laravel and vue experience in fintech"
OTHER = "we need a marketing manager to run paid acquisition campaigns across europe"


def test_fingerprint_is_stable_across_cosmetic_differences():
    a = fingerprint("Acme Ltd.", "Senior PHP Developer", "Kyiv, Ukraine", "full-time", "onsite")
    b = fingerprint("ACME  LLC", "Sr. PHP Developer",   "kyiv ukraine",  "full-time", "onsite")
    assert a == b


def test_remote_postings_ignore_location_entirely():
    a = fingerprint("Acme", "Senior PHP Developer", "Remote EU", "full-time", "remote")
    b = fingerprint("Acme", "Senior PHP Developer", "Remote",    "full-time", "remote")
    assert a == b


def test_onsite_postings_still_distinguish_location():
    a = fingerprint("Acme", "Senior PHP Developer", "Kyiv",   "full-time", "onsite")
    b = fingerprint("Acme", "Senior PHP Developer", "Warsaw", "full-time", "onsite")
    assert a != b


def test_employment_type_separates_otherwise_identical_roles():
    a = fingerprint("Acme", "Senior PHP Developer", "Remote", "full-time", "remote")
    b = fingerprint("Acme", "Senior PHP Developer", "Remote", "contract",  "remote")
    assert a != b


def test_jaccard_is_one_for_identical_text_and_zero_for_disjoint():
    assert jaccard(LONG, LONG) == 1.0
    assert jaccard(LONG, OTHER) < 0.2


def _merge(**overrides):
    kwargs = dict(
        existing_source_id=1, candidate_source_id=2,
        existing_last_seen=NOW, candidate_last_seen=NOW,
        existing_description=LONG, candidate_description=LONG,
    )
    kwargs.update(overrides)
    return can_merge(**kwargs)


def test_merges_across_two_sources_seen_together_with_similar_text():
    decision, reason = _merge()
    assert decision is True
    assert reason == "merged"


def test_never_merges_two_postings_from_the_same_source():
    decision, reason = _merge(candidate_source_id=1)
    assert decision is False
    assert "same source" in reason


def test_never_merges_across_a_repost_gap():
    decision, reason = _merge(candidate_last_seen=NOW + timedelta(days=95))
    assert decision is False
    assert "window" in reason


def test_never_merges_when_descriptions_diverge():
    decision, reason = _merge(candidate_description=OTHER)
    assert decision is False
    assert "similarity" in reason


def test_merges_when_a_description_is_missing_on_either_side():
    # The similarity gate can only apply when both sides have text.
    assert _merge(candidate_description=None)[0] is True
    assert _merge(existing_description="")[0] is True
