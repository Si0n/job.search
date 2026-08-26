from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from jobsearch.models import ParseResult, RawFetch, RawPosting

REQUIRED_FIELDS = ("external_id", "url", "title", "company")
# Selector field names that map onto a RawPosting attribute. Anything else a
# selectors row names is carried through in `meta` instead of being dropped, so
# a new hint costs a database row rather than a code change.
POSTING_FIELDS = frozenset(REQUIRED_FIELDS) | {
    "description", "location", "salary_raw", "posted_at",
    "arrangement_hint", "employment_hint",
}
# urljoin("https://djinni.co", "javascript:alert(1)") returns the scheme
# unchanged — a hostile href would otherwise land verbatim in job_sources.url.
# Checked here, not in store.upsert_posting, so one poisoned posting is
# skipped (counted in diagnostics["dropped"]) rather than raising mid-loop
# and killing the whole source via _harvest_source's broad catch.
ALLOWED_URL_SCHEMES = ("http", "https")
USER_AGENT = "jobsearch/0.1 (personal job search agent; contact via repository owner)"
TIMEOUT = httpx.Timeout(20.0)


class Adapter(ABC):
    name: str
    fetch_mode: str
    base_url: str

    @abstractmethod
    def fetch(self, query: dict, conditional: dict | None = None) -> RawFetch: ...

    @abstractmethod
    def parse(self, raw: RawFetch, selectors: dict) -> ParseResult: ...

    def _get(self, url: str, conditional: dict | None) -> RawFetch:
        return self._request("GET", url, conditional)

    def _post(self, url: str, conditional: dict | None, json_body: dict) -> RawFetch:
        """POST a JSON search body. Some boards expose their catalogue only this
        way; conditional headers are still sent but such endpoints do not
        answer 304, so every run fetches in full."""
        return self._request("POST", url, conditional, json_body)

    def _request(self, method: str, url: str, conditional: dict | None,
                 json_body: dict | None = None) -> RawFetch:
        headers = {"User-Agent": USER_AGENT}
        if conditional:
            if conditional.get("etag"):
                headers["If-None-Match"] = conditional["etag"]
            if conditional.get("last_modified"):
                headers["If-Modified-Since"] = conditional["last_modified"]

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = httpx.request(method, url, headers=headers, json=json_body,
                                         timeout=TIMEOUT, follow_redirects=True)
                # 304 is the conditional request working, not a failure. httpx
                # classes it as a redirect, so raise_for_status() rejects it and
                # the whole not-modified path below — which exists precisely so
                # an unchanged source is not mistaken for a vanished one — never
                # runs. Only servers that honour If-None-Match ever reach here.
                if response.status_code != 304:
                    response.raise_for_status()
                break
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)
        else:
            raise last_error

        return RawFetch(
            source_name=self.name,
            body=response.content,
            http_status=response.status_code,
            fetched_at=datetime.now(),
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )


def extract_field(node, spec: dict, base_url: str) -> str | None:
    found = node.select_one(spec["selector"])
    if found is None:
        return None

    attr = spec.get("attr", "text")
    if attr == "text":
        value = found.get_text(" ", strip=True)
    elif attr == "html":
        value = found.decode_contents()
    else:
        value = found.get(attr)

    if value is None:
        return None
    value = value.strip()
    if not value:
        return None

    if spec.get("regex"):
        match = re.search(spec["regex"], value)
        if not match:
            return None
        value = match.group(1)

    if spec.get("absolute"):
        value = urljoin(base_url, value)

    return value


class HtmlAdapter(Adapter):
    """Selector-driven HTML parsing shared by every http-html source.

    Subclasses supply build_url and, optionally, post_process. Nothing about the
    markup lives in code — it all comes from sources.selectors, which is what
    lets a repair change behavior without a deploy.
    """

    fetch_mode = "http-html"

    @abstractmethod
    def build_url(self, query: dict) -> str: ...

    def post_process(self, posting: RawPosting, node) -> RawPosting:
        return posting

    def fetch(self, query: dict, conditional: dict | None = None) -> RawFetch:
        return self._get(self.build_url(query or {}), conditional)

    def parse(self, raw: RawFetch, selectors: dict) -> ParseResult:
        soup = BeautifulSoup(raw.text(), "lxml")
        fields = selectors["fields"]
        minimum = int(selectors.get("minimum_items", 1))

        containers = soup.select(selectors["container"])
        diagnostics: dict = {
            "container_matched": len(containers),
            "item_matched": 0,
            "dropped": 0,
            "fill_rates": {},
        }

        if not containers:
            return ParseResult("broken", [], diagnostics)

        nodes = []
        for container in containers:
            nodes.extend(container.select(selectors["item"]))
        diagnostics["item_matched"] = len(nodes)

        if not nodes:
            marker = selectors.get("empty_state")
            if marker and soup.select_one(marker):
                return ParseResult("empty", [], diagnostics)
            return ParseResult("broken", [], diagnostics)

        postings: list[RawPosting] = []
        counts = {name: 0 for name in fields}

        for node in nodes:
            values = {
                name: extract_field(node, spec, self.base_url)
                for name, spec in fields.items()
            }
            for name, value in values.items():
                if value:
                    counts[name] += 1

            if any(values.get(name) is None for name in REQUIRED_FIELDS):
                diagnostics["dropped"] += 1
                continue
            if urlparse(values["url"]).scheme.lower() not in ALLOWED_URL_SCHEMES:
                diagnostics["dropped"] += 1
                continue

            posting = RawPosting(
                meta={k: v for k, v in values.items()
                      if k not in POSTING_FIELDS and v is not None},
                external_id=values["external_id"],
                url=values["url"],
                title=values["title"],
                company=values["company"],
                description=values.get("description") or "",
                location=values.get("location"),
                salary_raw=values.get("salary_raw"),
                arrangement_hint=values.get("arrangement_hint"),
                employment_hint=values.get("employment_hint"),
            )
            postings.append(self.post_process(posting, node))

        diagnostics["fill_rates"] = {
            name: round(count / len(nodes), 3) for name, count in counts.items()
        }

        if not postings:
            return ParseResult("broken", [], diagnostics)
        if len(postings) < minimum:
            return ParseResult("broken", postings, diagnostics)
        return ParseResult("ok", postings, diagnostics)


def _dig(payload, path: str):
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


_SALARY_PERIODS = {"yearly": "year", "annual": "year", "annually": "year",
                   "monthly": "month", "weekly": "week", "daily": "day",
                   "hourly": "hour"}


def _plain_amount(value) -> str:
    """Render a bound without a trailing ".0".

    JSON boards publish these as floats. "23520.0" is not a two-digit fraction,
    so the decimal rule below leaves it alone and the separator strip glues the
    zero on: 235200, ten times the real figure. The inflated number then also
    clears the magnitude threshold and flips the period from month to year.
    """
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.2f}"


def salary_range_text(low, high, currency: str = "$", period: str | None = None) -> str | None:
    """Render a board's numeric salary fields as the free text salary.parse reads.

    Boards publish salary as separate numbers; salary.parse takes one string.
    Three conversions here are load-bearing, and each fails silently — the job
    is lost, not flagged — if it is dropped:

    - A currency CODE needs a space. "USD 78000" parses; "USD78000" matches
      nothing and the salary is read as absent.
    - salary.parse knows the stems "month" and "hour" but not "monthly" or
      "hourly", and falls back to year. A monthly figure read as annual lands
      far below the salary floor, so the filter drops a job that in fact paid
      twelve times what was recorded.
    - A falsy bound means "not stated" and must yield no text at all. A zero
      figure reads as a real salary under the floor and loses the job the
      same way.
    """
    low, high = low or None, high or None
    if not low and not high:
        return None
    unit = f"/{_SALARY_PERIODS.get(period.lower(), period.lower())}" if period else ""
    money = f"{currency} " if currency and currency[-1].isalpha() else (currency or "")
    if low and high and low != high:
        return f"{money}{_plain_amount(low)} - {money}{_plain_amount(high)}{unit}"
    return f"{money}{_plain_amount(low or high)}{unit}"


class JsonAdapter(Adapter):
    """Path-driven parsing shared by every http-json source."""

    fetch_mode = "http-json"

    @abstractmethod
    def build_url(self, query: dict) -> str: ...

    def post_process(self, posting: RawPosting, item: dict) -> RawPosting:
        return posting

    def fetch(self, query: dict, conditional: dict | None = None) -> RawFetch:
        return self._get(self.build_url(query or {}), conditional)

    def parse(self, raw: RawFetch, selectors: dict) -> ParseResult:
        fields = selectors["fields"]
        diagnostics: dict = {"item_matched": 0, "dropped": 0, "fill_rates": {}}

        try:
            payload = json.loads(raw.text())
        except json.JSONDecodeError:
            diagnostics["reason"] = "invalid json"
            return ParseResult("broken", [], diagnostics)

        items = _dig(payload, selectors.get("root", ""))
        if not isinstance(items, list):
            diagnostics["reason"] = "root is not a list"
            return ParseResult("broken", [], diagnostics)

        if selectors.get("skip_first") and items:
            items = items[1:]
        diagnostics["item_matched"] = len(items)

        if not items:
            return ParseResult("empty", [], diagnostics)

        postings: list[RawPosting] = []
        counts = {name: 0 for name in fields}

        for item in items:
            values = {}
            for name, path in fields.items():
                value = _dig(item, path) if isinstance(item, dict) else None
                value = str(value).strip() if value not in (None, "") else None
                values[name] = value
                if value:
                    counts[name] += 1

            if any(values.get(name) is None for name in REQUIRED_FIELDS):
                diagnostics["dropped"] += 1
                continue
            if urlparse(values["url"]).scheme.lower() not in ALLOWED_URL_SCHEMES:
                diagnostics["dropped"] += 1
                continue

            postings.append(self.post_process(RawPosting(
                meta={k: v for k, v in values.items()
                      if k not in POSTING_FIELDS and v is not None},
                external_id=values["external_id"],
                url=values["url"],
                title=values["title"],
                company=values["company"],
                description=values.get("description") or "",
                location=values.get("location"),
                salary_raw=values.get("salary_raw"),
                arrangement_hint=values.get("arrangement_hint"),
                employment_hint=values.get("employment_hint"),
            ), item))

        diagnostics["fill_rates"] = {
            name: round(count / len(items), 3) for name, count in counts.items()
        }

        if not postings:
            return ParseResult("broken", [], diagnostics)
        return ParseResult("ok", postings, diagnostics)


def rss_to_items(text: str) -> list[dict]:
    """RSS <item> elements to the list-of-dicts shape JsonAdapter parses.

    Namespaces are stripped from tag names, so a field published as
    `job:salary` is reachable from selector JSON as plain `salary`. Without
    that, a prefixed field is silently invisible — LaraJobs puts salary,
    location and job_type behind a `job:` prefix, and they read as empty.

    Uses defusedxml: this is third-party XML fetched on a schedule, and the
    stdlib parser expands entities.
    """
    from defusedxml import ElementTree

    items = []
    root = ElementTree.fromstring(text)
    for item in root.iterfind(".//item"):
        entry = {child.tag.split("}")[-1]: (child.text or "") for child in item}
        entry["guid"] = entry.get("guid") or entry.get("link", "")
        items.append(entry)
    return items


class RssAdapter(JsonAdapter):
    """A feed source whose transport is RSS. Parsing stays JsonAdapter's — the
    conversion happens in fetch, so everything downstream sees one shape."""

    def fetch(self, query: dict, conditional: dict | None = None) -> RawFetch:
        raw = self._get(self.build_url(query or {}), conditional)
        if raw.http_status == 304:
            return raw
        return RawFetch(
            source_name=self.name,
            body=json.dumps(rss_to_items(raw.text())).encode("utf-8"),
            http_status=raw.http_status,
            fetched_at=raw.fetched_at,
            etag=raw.etag,
            last_modified=raw.last_modified,
        )
