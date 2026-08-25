import pytest

from jobsearch import normalize


@pytest.mark.parametrize("raw,expected", [
    ("Acme Ltd.", "acme"),
    ("Acme GmbH", "acme"),
    ("ACME, LLC", "acme"),
    ("Acme  Software   Sp. z o.o.", "acme software"),
    ("Acme Inc", "acme"),
])
def test_company_strips_legal_suffixes(raw, expected):
    assert normalize.company(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Senior PHP Developer", "senior php developer"),
    ("Senior  PHP   Developer  (Remote)", "senior php developer"),
    ("Senior PHP Developer — Fintech", "senior php developer fintech"),
    ("Sr. PHP Developer", "senior php developer"),
])
def test_title_normalization(raw, expected):
    assert normalize.title(raw) == expected


@pytest.mark.parametrize("hint,text,expected", [
    ("Full Remote", "", "remote"),
    (None, "This is a fully remote position.", "remote"),
    (None, "Hybrid, 2 days per week in the Kyiv office", "hybrid"),
    (None, "On-site in Warsaw", "onsite"),
    (None, "We are hiring a developer.", "unknown"),
])
def test_arrangement_detection(hint, text, expected):
    assert normalize.arrangement(hint, text) == expected


def test_arrangement_hint_beats_description_text():
    # The description mentions an office, but the source labelled it remote.
    assert normalize.arrangement("Remote", "Our office is in Berlin") == "remote"


def test_a_compound_hint_prefers_hybrid_over_remote():
    assert normalize.arrangement("Hybrid / Remote friendly", "") == "hybrid"


def test_hybrid_does_not_match_unrelated_substrings():
    assert normalize.arrangement(None, "Our hybridization strategy is unique") == "unknown"


@pytest.mark.parametrize("hint,text,expected", [
    ("Full-time", "", "full-time"),
    (None, "B2B contract, 12 months", "contract"),
    (None, "Part time, 20h/week", "part-time"),
    (None, "Summer internship programme", "internship"),
    (None, "Join our team", "unknown"),
])
def test_employment_detection(hint, text, expected):
    assert normalize.employment(hint, text) == expected


def test_internal_is_not_an_internship():
    assert normalize.employment(None, "We build internal tools and APIs.") == "unknown"
    assert normalize.employment(None, "Join our internal training programme, full-time.") == "full-time"


def test_description_strips_markup_and_collapses_whitespace():
    html = "<div><p>We need   <b>PHP</b>.</p>\n\n<p>And Vue.</p></div>"
    assert normalize.description(html) == "We need PHP.\n\nAnd Vue."


def test_description_hash_is_stable_across_whitespace_noise():
    a = normalize.description("<p>We need PHP.</p>")
    b = normalize.description("<p>We   need\tPHP.</p>")
    assert normalize.description_hash(a) == normalize.description_hash(b)


def test_location_normalization():
    assert normalize.location("  Kyiv, Ukraine ") == "kyiv ukraine"
    assert normalize.location(None) == ""
