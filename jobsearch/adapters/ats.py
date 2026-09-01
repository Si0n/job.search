from __future__ import annotations

import html as htmlmod
import json
import re
from datetime import datetime

import httpx

from jobsearch.adapters.base import JsonAdapter, RawFetch


def _greenhouse(item: dict) -> dict:
    """Greenhouse escapes its HTML a second time, so `content` arrives as
    "&lt;div&gt;". Unescaped here rather than left to normalize.description,
    which finds no tags in the encoded form and lets the markup through as
    visible text."""
    location = item.get("location") or {}
    return {
        "id": item.get("id"),
        "url": item.get("absolute_url"),
        "title": item.get("title"),
        "description": htmlmod.unescape(item.get("content") or ""),
        "location": location.get("name") if isinstance(location, dict) else None,
        "posted_at": item.get("first_published") or item.get("updated_at"),
    }


def _ashby(item: dict) -> dict:
    """`employmentType` is written without a separator ("FullTime"), which
    normalize.employment reads as unknown. Translated here so the source's own
    label survives instead of being re-guessed from the description."""
    employment = item.get("employmentType") or ""
    return {
        "id": item.get("id"),
        "url": item.get("jobUrl") or item.get("applyUrl"),
        "title": item.get("title"),
        "description": item.get("descriptionHtml") or item.get("descriptionPlain") or "",
        "location": item.get("location"),
        "posted_at": item.get("publishedAt"),
        "employment_hint": re.sub(r"(?<=[a-z])(?=[A-Z])", "-", employment).lower() or None,
        "arrangement_hint": item.get("workplaceType"),
    }


def _lever(item: dict) -> dict:
    """`createdAt` is milliseconds. Thirteen digits reach parse_posted_at as
    epoch seconds, overflow, and the posting loses its date entirely."""
    categories = item.get("categories") or {}
    created = item.get("createdAt")
    return {
        "id": item.get("id"),
        "url": item.get("hostedUrl") or item.get("applyUrl"),
        "title": item.get("text"),
        "description": item.get("description") or item.get("descriptionPlain") or "",
        "location": categories.get("location"),
        "posted_at": int(created) // 1000 if created else None,
        "employment_hint": categories.get("commitment"),
        "arrangement_hint": item.get("workplaceType"),
    }


# Each vendor publishes one board per employer: a URL template, where the
# postings sit in the response, and a reader that flattens one posting.
VENDORS = {
    "greenhouse": (
        "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
        "jobs", _greenhouse),
    "ashby": (
        "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "jobs", _ashby),
    "lever": (
        "https://api.lever.co/v0/postings/{slug}?mode=json",
        None, _lever),
}


class AtsAdapter(JsonAdapter):
    """The applicant-tracking boards of employers chosen for their domain.

    Every other source here is a job board: it publishes whoever pays to list,
    and the domain has to be recovered from the posting text afterwards. This
    one inverts that. The employers are named in `query`, so a posting's domain
    fit is settled before it is fetched — which is the whole point, since
    `domain_fit` is the dimension the aggregators score worst on.

    One source row covers sixty-odd boards across three ATS vendors. That costs
    a request per employer per run, and means a single dead board must not end
    the run: a board that fails is skipped and the rest still contribute.

    Keyword filtering happens here, not in the hard filters, for the reason the
    other keyword boards put `search_keywords` in their query — these companies
    are mostly Java and Go shops, and a run that returned all five thousand
    open roles would bury pass 1 exactly as the category dumps did.
    """

    name = "ats"
    base_url = "https://boards-api.greenhouse.io"

    def build_url(self, query: dict) -> str:
        query = query or {}
        template, _, _ = VENDORS[query.get("vendor", "greenhouse")]
        return template.format(slug=query.get("slug", ""))

    def fetch(self, query: dict, conditional: dict | None = None) -> RawFetch:
        """`conditional` is accepted and ignored: one ETag cannot stand for
        sixty-five boards, so every run reads them all and returns a synthetic
        200 rather than ever reporting not-modified."""
        query = query or {}
        keywords = query.get("keywords") or []
        fallback = query.get("fallback_keywords") or []
        excluded = query.get("exclude_title_keywords") or []

        items: list[dict] = []
        fetched_at = None
        for vendor in VENDORS:
            for slug, company in (query.get(vendor) or {}).items():
                try:
                    raw = self._get(self.build_url({"vendor": vendor, "slug": slug}), None)
                except httpx.HTTPError:
                    # A board that 404s or times out costs its own postings and
                    # nothing else. Recorded as a lower item count rather than
                    # an exception, which _harvest_source would read as the
                    # whole source being down.
                    continue
                fetched_at = fetched_at or raw.fetched_at
                for record in self._records(vendor, slug, company, raw.text()):
                    if self._wanted(record, keywords, fallback, excluded):
                        items.append(record)

        return RawFetch(
            source_name=self.name,
            body=json.dumps(items).encode("utf-8"),
            http_status=200,
            fetched_at=fetched_at or datetime.now(),
        )

    @staticmethod
    def _records(vendor: str, slug: str, company: str, payload: str) -> list[dict]:
        """One board's response to flat, pipeline-shaped dicts.

        The company is the configured display name, never the vendor's own
        field: Greenhouse publishes a per-job `company_name` that is a
        department on some boards, so trusting it files postings under
        employers that do not exist.
        """
        _, root, read = VENDORS[vendor]
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
        raw_items = payload.get(root) if root else payload
        if not isinstance(raw_items, list):
            return []

        records = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            record = read(item)
            if not (record.get("id") and record.get("url") and record.get("title")):
                continue
            record["external_id"] = f"{vendor}-{slug}-{record.pop('id')}"
            record["company"] = company
            records.append({k: v for k, v in record.items() if v not in (None, "")})
        return records

    @staticmethod
    def _wanted(record: dict, keywords: list[str], fallback: list[str],
                excluded: list[str] = ()) -> bool:
        """A posting worth scoring. Three lists, applied in order.

        `excluded` vetoes on the TITLE, and vetoes everything — a support role
        that happens to name PHP in its requirements is still a support role.
        Measured over one run of all sixty-five boards, this is the rule that
        separates Mollie's "Application Engineer II" from its "Technical
        Support Specialist": both mention PHP, only one is the job.

        `fallback` also matches the TITLE ONLY. Every engineering description at
        a payments company says "backend" somewhere, so matching the body on it
        admits the whole board — the category-dump failure that took remoteok
        and weworkremotely offline.

        `keywords` are stack signals and match anywhere, because these companies
        do not put PHP in a title even when the role is a PHP role.
        """
        title = (record.get("title") or "").lower()
        if any(word.lower() in title for word in excluded):
            return False
        if any(word.lower() in title for word in fallback):
            return True
        if not keywords:
            return not fallback
        body = title + " " + (record.get("description") or "").lower()
        return any(word.lower() in body for word in keywords)
