from __future__ import annotations

import json

from jobsearch.adapters.base import JsonAdapter, salary_range_text
from jobsearch.models import RawFetch, RawPosting


class NoFluffJobsAdapter(JsonAdapter):
    """Poland's mandatory salary disclosure makes this the only source here that
    states a figure on effectively every posting. It is reached by POSTing a
    search body rather than by a query string, and `salaryCurrency` is required
    — omitting it answers 400, which reads like a block but is not one."""

    name = "nofluffjobs"
    base_url = "https://nofluffjobs.com"

    def build_url(self, query: dict) -> str:
        return (f"{self.base_url}/api/search/posting"
                f"?limit={int(query.get('limit', 100))}"
                f"&salaryCurrency={query.get('salary_currency', 'PLN')}"
                f"&salaryPeriod={query.get('salary_period', 'month')}"
                f"&region={query.get('region', 'pl')}")

    def build_body(self, query: dict) -> dict:
        """Only the keys this API actually honours.

        It answers 200 and silently drops any criterion it does not recognise:
        a `technology` filter returns the whole unfiltered backend catalogue,
        775 postings of mostly Java, with nothing in the response marking the
        criterion as ignored. `requirement` is the key that filters by stack.
        """
        criteria = {k: v for k, v in (
            ("category", query.get("category")),
            ("requirement", query.get("requirement")),
            ("seniority", query.get("seniority")),
        ) if v}
        return {"criteriaSearch": criteria}

    def _describe(self, detail: dict) -> str:
        """Build a description from the detail endpoint's structured fields.

        The search response carries no description at all, so without this every
        posting reaches pass-2 scoring with an empty body and is judged on its
        title alone.
        """
        parts = []
        requirements = detail.get("requirements") or {}
        for label, key in (("Must have", "musts"), ("Nice to have", "nices")):
            values = [r.get("value") for r in requirements.get(key) or [] if r.get("value")]
            if values:
                parts.append(f"{label}: {', '.join(values)}.")
        tasks = (detail.get("specs") or {}).get("dailyTasks") or []
        if tasks:
            parts.append("Daily tasks: " + " ".join(f"- {t}" for t in tasks))
        return " ".join(parts)

    def fetch(self, query: dict, conditional: dict | None = None) -> RawFetch:
        """Collapse regional duplicates, then fill in the missing descriptions.

        This board lists one job once per Polish voivodeship: 174 rows in a
        single response are 13 actual postings, one of them repeated 34 times.
        Each copy carries its own id and URL, so nothing downstream treats them
        as the same job -- same-source postings are deliberately never merged --
        and the scoring queue fills up with the same role over and over.
        `reference` is the identity that survives the split, so it is the
        external_id; the fullyRemote copy is the one worth keeping.

        The API returns `url` as a bare slug. parse() rejects a URL whose
        scheme is not http(s) — a security boundary that has to see the final
        value, so it cannot be left for post_process to fix afterwards. Doing
        it here keeps that check meaningful and matches how RssAdapter
        reshapes a transport quirk before parsing rather than during it.
        """
        query = query or {}
        raw = self._post(self.build_url(query), conditional, self.build_body(query))
        payload = json.loads(raw.text())

        collapsed: dict[str, dict] = {}
        for item in payload.get("postings", []):
            reference = item.get("reference")
            if not reference:
                continue
            kept = collapsed.get(reference)
            if kept is None or (item.get("fullyRemote") and not kept.get("fullyRemote")):
                collapsed[reference] = item

        postings = []
        for reference, item in collapsed.items():
            item["reference"] = reference
            slug = item.get("url") or ""
            if slug and not slug.startswith(("http://", "https://")):
                item["url"] = f"{self.base_url}/job/{slug.lstrip('/')}"
            # One request per real posting, not per regional copy. A detail
            # fetch that fails costs this posting its description and nothing
            # else -- absent data never fails anything downstream.
            try:
                detail = json.loads(self._get(
                    f"{self.base_url}/api/posting/{reference}", None).text())
                item["description"] = self._describe(detail)
            except Exception:
                item["description"] = ""
            postings.append(item)
        payload["postings"] = postings

        return RawFetch(
            source_name=self.name,
            body=json.dumps(payload).encode("utf-8"),
            http_status=raw.http_status,
            fetched_at=raw.fetched_at,
            etag=raw.etag,
            last_modified=raw.last_modified,
        )

    def post_process(self, posting: RawPosting, item: dict) -> RawPosting:
        pay = item.get("salary") or {}
        posting.salary_raw = salary_range_text(
            pay.get("from"), pay.get("to"),
            currency=pay.get("currency") or "PLN",
            period=pay.get("period"),
        )
        if item.get("fullyRemote"):
            posting.arrangement_hint = "remote"
        return posting
