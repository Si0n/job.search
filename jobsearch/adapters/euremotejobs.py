from __future__ import annotations

from jobsearch.adapters.base import WpJobFeedAdapter
from jobsearch.models import RawPosting


class EuRemoteJobsAdapter(WpJobFeedAdapter):
    """EU-facing remote board. Small feed, but every item answers the keyword,
    and it publishes the employer's full posting body — which several of the
    same roles do not have on the aggregators that also carry them."""

    name = "euremotejobs"
    base_url = "https://euremotejobs.com"

    def post_process(self, posting: RawPosting, item: dict) -> RawPosting:
        """The board lists remote roles only, so state that rather than leaving
        arrangement to be inferred from a location like 'Europe, US, UK'."""
        posting.arrangement_hint = "remote"
        return posting
