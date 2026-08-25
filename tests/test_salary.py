import pytest

from jobsearch.config import DEFAULT_RATES
from jobsearch.salary import Salary, parse, to_monthly_eur


@pytest.mark.parametrize("raw,expected", [
    ("€5,000/month",  Salary(5000, 5000, "EUR", "month", "unknown", "posting")),
    ("€60,000/year",  Salary(60000, 60000, "EUR", "year", "unknown", "posting")),
    ("$100k-$130k",   Salary(100000, 130000, "USD", "year", "unknown", "posting")),
    ("$40-$60/hour",  Salary(40, 60, "USD", "hour", "unknown", "posting")),
    ("PLN 25,000",    Salary(25000, 25000, "PLN", "month", "unknown", "posting")),
    ("£70k",          Salary(70000, 70000, "GBP", "year", "unknown", "posting")),
    ("€500/day",      Salary(500, 500, "EUR", "day", "contractor", "posting")),
    ("up to €8k",     Salary(None, 8000, "EUR", "month", "unknown", "posting")),
    ("from $5000",    Salary(5000, None, "USD", "month", "unknown", "posting")),
    ("3000-5000 EUR", Salary(3000, 5000, "EUR", "month", "unknown", "posting")),
])
def test_parse_the_format_zoo(raw, expected):
    assert parse(raw) == expected


@pytest.mark.parametrize("raw", ["competitive", "negotiable", "DOE", "", None, "attractive package"])
def test_unparseable_compensation_is_absent_not_zero(raw):
    result = parse(raw)
    assert result.source == "absent"
    assert result.min is None and result.max is None


def test_day_rate_implies_contractor():
    assert parse("€500/day").type == "contractor"
    assert parse("$60/hour").type == "contractor"


@pytest.mark.parametrize("raw,expected_eur", [
    ("€5,000/month", 5000),
    ("€60,000/year", 5000),
    ("$120,000/year", 9200),
    ("£70k", 6825),
    ("€500/day", 10500),      # 21 working days
    ("$50/hour", 7728),       # 168 working hours: 50 * 168 * 0.92
])
def test_monthly_eur_derivation(raw, expected_eur):
    assert to_monthly_eur(parse(raw), DEFAULT_RATES) == expected_eur


def test_monthly_eur_uses_the_midpoint_of_a_range():
    assert to_monthly_eur(parse("€4,000-€6,000/month"), DEFAULT_RATES) == 5000


def test_monthly_eur_is_none_when_absent():
    assert to_monthly_eur(parse("competitive"), DEFAULT_RATES) is None


def test_monthly_eur_is_none_for_unknown_currency():
    assert to_monthly_eur(Salary(5000, 5000, "XYZ", "month", "unknown", "posting"), DEFAULT_RATES) is None


@pytest.mark.parametrize("raw,expected", [
    ("5+ years experience, €80,000/year",         Salary(80000, 80000, "EUR", "year", "unknown", "posting")),
    ("Team of 12 engineers. Salary €7,000/month", Salary(7000, 7000, "EUR", "month", "unknown", "posting")),
    ("€6,000/month, 25 days holiday",             Salary(6000, 6000, "EUR", "month", "unknown", "posting")),
    ("Senior dev, 3-5 years, $90k-$110k",         Salary(90000, 110000, "USD", "year", "unknown", "posting")),
])
def test_parse_ignores_digits_unrelated_to_the_money_expression(raw, expected):
    # salary_raw is scraped free text — years of experience, headcounts, and PTO
    # days sit right next to the real figure. Only digits anchored to a currency
    # marker may enter the parsed range.
    assert parse(raw) == expected


def test_weak_currency_threshold_is_a_documented_approximation():
    # Below the threshold with no period marker, UAH reads as monthly. A genuine
    # annual figure in this window is overstated ~12x — accepted, because the
    # failure admits a bad job rather than deleting a good one.
    assert parse("UAH 800,000").period == "month"
    assert parse("UAH 950,000").period == "year"
    assert parse("UAH 800,000/year").period == "year"   # explicit marker always wins


@pytest.mark.parametrize("raw,expected_min,expected_max", [
    ("€6000-8000/month", 6000, 8000),   # ASCII hyphen  U+002D
    ("€6000–8000/month", 6000, 8000),  # en dash    U+2013
    ("€6000—8000/month", 6000, 8000),  # em dash    U+2014
    ("€6000−8000/month", 6000, 8000),  # minus sign U+2212
])
def test_range_separators_are_all_equivalent(raw, expected_min, expected_max):
    result = parse(raw)
    assert (result.min, result.max) == (expected_min, expected_max)


def test_a_bare_numeric_range_does_not_bridge_into_a_later_currency_figure():
    # The years range must not merge with the salary range.
    assert parse("Senior dev, 3-5 years, $90k-$110k").min == 90000
