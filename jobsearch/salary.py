from __future__ import annotations

import re
from dataclasses import dataclass

WORKING_DAYS_PER_MONTH = 21
WORKING_HOURS_PER_MONTH = 168

_CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "₴": "UAH", "zł": "PLN"}
_CURRENCY_CODES = ("EUR", "USD", "GBP", "PLN", "UAH", "CHF")

_PERIOD_PATTERNS = (
    ("hour", re.compile(r"/\s*(hour|hr|h)\b|\bper\s+hour\b", re.I)),
    ("day", re.compile(r"/\s*(day|d)\b|\bper\s+day\b", re.I)),
    ("month", re.compile(r"/\s*(month|mo|mth)\b|\bper\s+month\b", re.I)),
    ("year", re.compile(r"/\s*(year|yr|annum|a)\b|\bper\s+year\b|\bp\.?a\.?\b", re.I)),
)

_AMOUNT = re.compile(r"(\d[\d\s,._]*)\s*(k)?", re.I)
_UP_TO = re.compile(r"\b(up\s+to|максимум|до)\b", re.I)
_FROM = re.compile(r"\b(from|starting|від|от)\b", re.I)

# Magnitude fallback threshold, expressed in each currency's own units. PLN and
# UAH carry far more nominal units per EUR than EUR/USD/GBP/CHF, so a flat
# 20,000 cutoff would misread a plausible PLN/UAH *monthly* figure as annual.
_YEAR_MAGNITUDE_THRESHOLD = 20000
_WEAK_CURRENCY_THRESHOLD = {"PLN": 90000, "UAH": 900000}


@dataclass(frozen=True)
class Salary:
    min: int | None
    max: int | None
    currency: str | None
    period: str | None
    type: str
    source: str


ABSENT = Salary(None, None, None, None, "unknown", "absent")


def _currency(text: str) -> str | None:
    upper = text.upper()
    for code in _CURRENCY_CODES:
        if re.search(rf"\b{code}\b", upper):
            return code
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    return None


def _amounts(text: str) -> list[int]:
    found: list[int] = []
    for match in _AMOUNT.finditer(text):
        digits = re.sub(r"[\s,._]", "", match.group(1))
        if not digits:
            continue
        value = int(digits)
        if match.group(2):
            value *= 1000
        found.append(value)
    return found


def _period(text: str, amounts: list[int], currency: str | None) -> str:
    for name, pattern in _PERIOD_PATTERNS:
        if pattern.search(text):
            return name
    # No explicit period. Magnitude is the only signal left, and it is a reliable
    # one: nobody is paid 60,000 a month or 5,000 a year in these markets.
    threshold = _WEAK_CURRENCY_THRESHOLD.get(currency, _YEAR_MAGNITUDE_THRESHOLD)
    biggest = max(amounts) if amounts else 0
    return "year" if biggest >= threshold else "month"


def parse(raw: str | None) -> Salary:
    if not raw or not raw.strip():
        return ABSENT

    text = raw.strip()
    amounts = _amounts(text)
    currency = _currency(text)
    if not amounts or currency is None:
        return ABSENT

    period = _period(text, amounts, currency)

    low: int | None
    high: int | None
    if _UP_TO.search(text):
        low, high = None, max(amounts)
    elif _FROM.search(text):
        low, high = min(amounts), None
    elif len(amounts) >= 2:
        low, high = min(amounts), max(amounts)
    else:
        low = high = amounts[0]

    # A day/hour rate implies contractor billing only when it's a single flat
    # figure; a day/hour *range* reads as a pay band, not a contract rate.
    kind = "contractor" if period in ("hour", "day") and low is not None and low == high else "unknown"

    return Salary(low, high, currency, period, kind, "posting")


def to_monthly_eur(s: Salary, rates: dict[str, float]) -> int | None:
    if s.source == "absent" or s.currency is None or s.period is None:
        return None
    rate = rates.get(s.currency)
    if rate is None:
        return None

    figures = [v for v in (s.min, s.max) if v is not None]
    if not figures:
        return None
    amount = sum(figures) / len(figures)

    per_month = {
        "month": 1.0,
        "year": 1 / 12,
        "day": float(WORKING_DAYS_PER_MONTH),
        "hour": float(WORKING_HOURS_PER_MONTH),
    }[s.period]

    return int(round(amount * per_month * rate))
