from __future__ import annotations

from jobsearch.adapters.base import JsonAdapter, salary_range_text
from jobsearch.models import RawPosting


class JobicyAdapter(JsonAdapter):
    name = "jobicy"
    base_url = "https://jobicy.com"

    def build_url(self, query: dict) -> str:
        params = [f"count={int(query.get('count', 50))}"]
        if tag := query.get("tag"):
            params.append(f"tag={tag}")
        if geo := query.get("geo"):
            params.append(f"geo={geo}")
        if industry := query.get("industry"):
            params.append(f"industry={industry}")
        return f"{self.base_url}/api/v2/remote-jobs?" + "&".join(params)

    def post_process(self, posting: RawPosting, item: dict) -> RawPosting:
        # Jobicy is a remote-only board, so the arrangement is a property of
        # the board rather than of any field selectors could point at.
        posting.arrangement_hint = "remote"

        # The reason this source was added: it states a figure on roughly three
        # quarters of its listings, in four currencies and three periods. All of
        # that has to survive into one string — see salary_range_text.
        posting.salary_raw = salary_range_text(
            item.get("salaryMin"), item.get("salaryMax"),
            currency=item.get("salaryCurrency") or "$",
            period=item.get("salaryPeriod"),
        )
        return posting
