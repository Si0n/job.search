from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from jobsearch.models import ParseResult, RawFetch, RawPosting

REQUIRED_FIELDS = ("external_id", "url", "title", "company")
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
        headers = {"User-Agent": USER_AGENT}
        if conditional:
            if conditional.get("etag"):
                headers["If-None-Match"] = conditional["etag"]
            if conditional.get("last_modified"):
                headers["If-Modified-Since"] = conditional["last_modified"]

        response = httpx.get(url, headers=headers, timeout=TIMEOUT, follow_redirects=True)
        response.raise_for_status()
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

            posting = RawPosting(
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
