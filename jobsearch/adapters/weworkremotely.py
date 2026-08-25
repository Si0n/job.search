from __future__ import annotations

import json

from defusedxml import ElementTree

from jobsearch.adapters.base import JsonAdapter
from jobsearch.models import RawFetch


class WeWorkRemotelyAdapter(JsonAdapter):
    """RSS in, JSON-shaped out.

    Converting in fetch keeps one parse implementation for every feed source.
    A third base class for a single RSS site would be an abstraction with one
    user, and repair would then have two code paths to reason about.
    """

    name = "weworkremotely"
    base_url = "https://weworkremotely.com"

    def build_url(self, query: dict) -> str:
        category = query.get("category", "remote-programming-jobs")
        return f"{self.base_url}/categories/{category}.rss"

    def fetch(self, query: dict, conditional: dict | None = None) -> RawFetch:
        raw = self._get(self.build_url(query or {}), conditional)
        items = []
        root = ElementTree.fromstring(raw.text())
        for item in root.iterfind(".//item"):
            entry = {child.tag.split("}")[-1]: (child.text or "") for child in item}
            entry["guid"] = entry.get("guid") or entry.get("link", "")
            items.append(entry)

        return RawFetch(
            source_name=self.name,
            body=json.dumps(items).encode("utf-8"),
            http_status=raw.http_status,
            fetched_at=raw.fetched_at,
            etag=raw.etag,
            last_modified=raw.last_modified,
        )

    def post_process(self, posting, item):
        # weworkremotely.com is a remote-only board by definition — every
        # posting on it is remote. That is a property of the BOARD, not of
        # the feed's markup, so it belongs in code rather than selectors.json
        # (which has no way to express a constant).
        posting.arrangement_hint = "remote"

        # The feed fuses company and role into one <title> element
        # ("Gusto, Inc.: Staff Software Engineer, ..."). Selectors map both
        # `title` and `company` to that same element so the required-field
        # check (which runs before post_process) sees both populated; this
        # splits them apart on the FIRST colon, which is WWR's convention —
        # a colon inside the company name itself (rare) will mis-split, and
        # there is no better heuristic available without an employer list.
        company, sep, title = posting.title.partition(":")
        if sep:
            posting.company = company.strip()
            posting.title = title.strip()
        else:
            # No separator: `company` currently holds the whole fused string
            # (both fields were mapped to the same selector so the
            # required-field check would pass). We don't know the employer —
            # leaving the fused string in place would put the job title where
            # the employer belongs, corrupting the dedupe fingerprint and the
            # scorer's company_fit dimension. Blank is honest; wrong is not.
            posting.company = ""
        return posting
