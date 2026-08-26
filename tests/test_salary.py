import pytest

from jobsearch.config import DEFAULT_RATES
from jobsearch.salary import Salary, parse, to_monthly_eur
from jobsearch.adapters.base import salary_range_text


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


# --- Numeric board fields -> the free text salary.parse reads back ----------
# These assert a ROUND TRIP rather than a string shape: every bug this helper
# exists to prevent is a formatting choice that parses back to the wrong number
# (or to nothing) and silently loses the job, with no error anywhere.

def test_range_text_round_trips_through_the_parser():
    text = salary_range_text(78000, 83500, currency="USD", period="yearly")
    parsed = parse(text)
    assert (parsed.min, parsed.max) == (78000, 83500)
    assert parsed.currency == "USD" and parsed.period == "year"


def test_an_alphabetic_currency_code_is_separated_from_the_digits():
    # "USD78000" matches nothing and parses as absent, so the salary vanishes.
    parsed = parse(salary_range_text(78000, 83500, currency="USD"))
    assert parsed.min == 78000, "currency code glued to the amount loses the salary"


def test_a_currency_symbol_is_not_padded():
    assert salary_range_text(1000, 2000, currency="$") == "$1000 - $2000"


def test_monthly_is_not_silently_widened_to_annual():
    # The parser knows the stem "month" but not "monthly". Left as "monthly" a
    # 9000/month role is recorded as 9000/year — 750/month — and the salary
    # floor drops it, which looks identical to the job never existing.
    parsed = parse(salary_range_text(9000, None, currency="EUR", period="monthly"))
    assert parsed.period == "month"
    assert parsed.min == 9000


def test_hourly_is_not_silently_widened_to_annual():
    parsed = parse(salary_range_text(85, None, currency="USD", period="hourly"))
    assert parsed.period == "hour"


def test_equal_bounds_render_as_one_figure():
    assert salary_range_text(5000, 5000, currency="$") == "$5000"


def test_a_lone_bound_renders_alone():
    assert salary_range_text(None, 5000, currency="$") == "$5000"
    assert salary_range_text(5000, None, currency="$") == "$5000"


def test_absent_and_zero_bounds_produce_no_text_at_all():
    # Zero means "not stated" on these boards; "$0" would read as a real salary
    # below the floor and lose the job exactly like a mis-scaled period does.
    assert salary_range_text(None, None) is None
    assert salary_range_text(0, 0) is None
    assert salary_range_text(0, None, currency="USD", period="yearly") is None


# --- Currencies for the boards outside the PL/UA market ---------------------

def test_canadian_dollars_are_recognised():
    # Jobicy states four Canadian salaries in a single page of results; before
    # CAD was known they parsed as absent, so the figures were dropped while
    # the postings themselves arrived looking complete.
    parsed = parse("CAD 140000 - CAD 160000/year")
    assert (parsed.min, parsed.max, parsed.currency) == (140000, 160000, "CAD")


def test_shekels_are_recognised_by_code_and_symbol():
    assert parse("ILS 45000 per month").currency == "ILS"
    assert parse("₪45000 per month").currency == "ILS"


def test_a_monthly_shekel_salary_is_not_read_as_annual():
    # 45,000 ILS/month is an ordinary senior salary in Tel Aviv and clears the
    # flat 20,000 magnitude cutoff. Read as annual it becomes 3,750/month,
    # about 940 EUR, and the salary floor deletes the job.
    assert parse("₪45000").period == "month"


def test_a_canadian_annual_salary_is_still_read_as_annual():
    assert parse("CAD 140000").period == "year"


def test_the_new_currencies_convert_to_monthly_eur():
    for text in ("CAD 120000 per year", "ILS 45000 per month"):
        assert to_monthly_eur(parse(text), DEFAULT_RATES) is not None


def test_decimal_cents_are_not_glued_onto_the_amount():
    # LaraJobs states "USD$134,450.00 - USD $167,258.30". Stripping the decimal
    # point along with the thousands separators turned that into 13,445,000 —
    # a hundredfold overstatement that sorts to the top of the dashboard and
    # makes compensation_fit meaningless.
    parsed = parse("USD$134,450.00 - USD $167,258.30")
    assert (parsed.min, parsed.max) == (134450, 167258)


def test_a_european_decimal_comma_is_a_fraction_not_a_thousands_group():
    assert parse("EUR 1.234,56 per month").min == 1234


def test_a_three_digit_group_is_still_a_thousands_separator():
    assert parse("EUR 6.500 per month").min == 6500
    assert parse("$134,450").min == 134450


def test_a_float_bound_does_not_gain_a_trailing_zero():
    # NoFluffJobs publishes salary as JSON floats. "PLN 23520.0" strips to
    # 235200 — ten times the figure — and the inflated number also clears the
    # PLN magnitude threshold, so a monthly salary is recorded as annual.
    parsed = parse(salary_range_text(23520.0, 28560.0, currency="PLN", period="Month"))
    assert (parsed.min, parsed.max) == (23520, 28560)
    assert parsed.period == "month"


def test_a_genuinely_fractional_bound_keeps_its_value():
    assert parse(salary_range_text(85.5, None, currency="USD", period="hourly")).min == 85
