from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import urlencode

from jobsearch.adapters.base import HtmlAdapter

# DOU writes the month as a Ukrainian genitive noun and omits the year entirely.
_MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}
_RELATIVE = {"сьогодні": 0, "вчора": 1}
_DATE = re.compile(r"(\d{1,2})\s+([а-яіїєґ']+)")


def parse_dou_date(text: str | None, today: datetime | None = None) -> datetime | None:
    """Read DOU's '17 серпня' into a datetime, inferring the missing year.

    The markup carries a day and a month name and no year at all, so the year
    has to be inferred. A date later than today must belong to last year:
    reading 'грудня' as this December would date a posting nine months into the
    future, and an age filter measuring against it would call it fresh forever.
    """
    if not text:
        return None
    text = text.strip().lower()

    for word, days_back in _RELATIVE.items():
        if text.startswith(word):
            base = today or datetime.now()
            return (base - timedelta(days=days_back)).replace(
                hour=0, minute=0, second=0, microsecond=0)

    match = _DATE.search(text)
    if not match:
        return None
    month = _MONTHS.get(match.group(2))
    if month is None:
        return None

    base = today or datetime.now()
    try:
        posted = datetime(base.year, month, int(match.group(1)))
    except ValueError:
        return None
    if posted.date() > base.date():
        posted = posted.replace(year=base.year - 1)
    return posted


class DouAdapter(HtmlAdapter):
    name = "dou"
    base_url = "https://jobs.dou.ua"

    def build_url(self, query: dict) -> str:
        params = {"category": query.get("category", "PHP")}
        if query.get("remote"):
            params["remote"] = ""
        if query.get("experience"):
            params["exp"] = query["experience"]
        return f"{self.base_url}/vacancies/?{urlencode(params)}"

    def post_process(self, posting, node):
        # Set here rather than through a selector because base.parse_posted_at
        # reads machine formats — ISO, RFC 2822, epoch — and DOU publishes a
        # Ukrainian month name with no year, which none of those cover.
        date_node = node.select_one(".date")
        posting.posted_at = parse_dou_date(date_node.get_text(" ", strip=True) if date_node else None)
        return posting
