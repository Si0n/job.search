from __future__ import annotations

from jobsearch.adapters.base import RssAdapter
from jobsearch.models import RawPosting


class LaraJobsAdapter(RssAdapter):
    """Laravel-specific board. Small feed, but it publishes a structured salary
    on roughly half its postings — against about 4% across the other sources."""

    name = "larajobs"
    base_url = "https://larajobs.com"

    def build_url(self, query: dict) -> str:
        return f"{self.base_url}/feed"

    def post_process(self, posting: RawPosting, item: dict) -> RawPosting:
        """Describe the role from the feed's own fields when it ships no body.

        LaraJobs publishes an empty CDATA `<description>` and redirects `link`
        to the employer's own site, so there is no posting body to fetch from a
        predictable place. The feed does carry the stack in `tags`, which the
        selectors have no field to put. Restating those fields here keeps the
        job scoreable instead of arriving with an empty description, and says
        plainly that the text is feed metadata so pass 2 neither penalises the
        role for a thin posting nor reads detail into it that isn't there.
        """
        if posting.description and posting.description.strip():
            return posting
        parts = []
        if tags := (item.get("tags") or "").strip():
            parts.append(f"Tags: {tags.replace(',', ', ')}.")
        if location := (item.get("location") or "").strip():
            parts.append(f"Location: {location}.")
        if job_type := (item.get("job_type") or "").strip():
            parts.append(f"Type: {job_type.replace('_', ' ').title()}.")
        parts.append("LaraJobs lists this role without a description; the full "
                     "posting is on the employer's own site.")
        posting.description = " ".join(parts)
        return posting
