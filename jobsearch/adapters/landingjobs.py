from __future__ import annotations

import json
import re

from jobsearch.adapters.base import JsonAdapter, RawFetch

_COMPANY_FROM_URL = re.compile(r"/at/([^/]+)/")


class LandingJobsAdapter(JsonAdapter):
    """European tech board (Portugal-based, EU-wide) with a keyword-searchable
    API and a structured gross salary band on most listings — coverage matched
    only by nofluffjobs and jobicy among the sources here.

    Two fields the pipeline needs are not fields at all in this API: the company
    is only present as a slug inside the posting URL, and the salary arrives as
    three separate numbers. Both are assembled in `fetch` rather than
    `post_process`, because JsonAdapter drops a record missing any required
    field BEFORE post_process runs — a company reconstructed afterwards would
    arrive too late to save the row.
    """

    name = "landingjobs"
    base_url = "https://landing.jobs"

    def build_url(self, query: dict) -> str:
        query = query or {}
        keyword = query.get("q", "php")
        limit = int(query.get("limit", 50))
        return f"{self.base_url}/api/v1/jobs?q={keyword}&limit={limit}"

    def fetch(self, query: dict, conditional: dict | None = None) -> RawFetch:
        raw = self._get(self.build_url(query or {}), conditional)
        if raw.http_status == 304:
            return raw
        try:
            items = json.loads(raw.text())
        except json.JSONDecodeError:
            # Leave it alone and let parse() classify it broken, so a rate-limit
            # HTML page is diagnosed in one place rather than two.
            return raw
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    self._enrich(item)
        return RawFetch(
            source_name=self.name,
            body=json.dumps(items).encode("utf-8"),
            http_status=raw.http_status,
            fetched_at=raw.fetched_at,
            etag=raw.etag,
            last_modified=raw.last_modified,
        )

    @staticmethod
    def _enrich(item: dict) -> None:
        """Derive company, salary_raw, location and arrangement onto the record.

        A posting whose URL does not carry a company slug keeps no company and
        is dropped by parse() with a reason in diagnostics — better than
        inventing a placeholder that would later read as a real employer.
        """
        if match := _COMPANY_FROM_URL.search(str(item.get("url") or "")):
            item["company"] = match.group(1).replace("-", " ").title()

        low, high = item.get("gross_salary_low"), item.get("gross_salary_high")
        currency = item.get("currency_code") or "EUR"
        if low and high:
            item["salary_raw"] = f"{low} - {high} {currency}/year"
        elif low or high:
            item["salary_raw"] = f"{low or high} {currency}/year"

        cities = [
            (loc.get("city") or loc.get("country_code") or "").strip()
            for loc in (item.get("locations") or []) if isinstance(loc, dict)
        ]
        if cities := [c for c in cities if c]:
            item["location"] = ", ".join(dict.fromkeys(cities))

        if item.get("remote"):
            item["arrangement_hint"] = "remote"

        body = [item.get("role_description") or "", item.get("main_requirements") or ""]
        if tags := item.get("tags"):
            body.append("Tags: " + ", ".join(str(t) for t in tags) + ".")
        if text := "\n\n".join(part for part in body if part):
            item["description"] = text
