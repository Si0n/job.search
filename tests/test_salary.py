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
