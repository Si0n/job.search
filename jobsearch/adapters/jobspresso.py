from __future__ import annotations

from jobsearch.adapters.base import WpJobFeedAdapter
from jobsearch.models import RawPosting


class JobspressoAdapter(WpJobFeedAdapter):
    """US and EU remote board, curated rather than aggregated.

    Its `job_type` field is NOT mapped to employment_hint in the selectors: the
    board files roles under marketing categories regardless of the work — the
    first PHP backend role in the feed is typed "Marketing" — so trusting it
    would hand the employment filter a value the posting never claimed. The
    field still travels in `meta`, where a wrong label costs nothing.
    """

    name = "jobspresso"
    base_url = "https://jobspresso.co"

    def post_process(self, posting: RawPosting, item: dict) -> RawPosting:
        posting.arrangement_hint = "remote"
        return posting
