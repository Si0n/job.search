from __future__ import annotations

from urllib.parse import urlencode

from jobsearch.adapters.base import JsonAdapter
from jobsearch.models import RawPosting


class RemotiveAdapter(JsonAdapter):
    """Remote-first board with unusually good salary coverage — about two thirds
    of software-dev postings carry a figure, mostly at US rates."""

    name = "remotive"
    base_url = "https://remotive.com"

    def build_url(self, query: dict) -> str:
        params = {"category": query.get("category", "software-dev")}
        if query.get("search"):
            params["search"] = query["search"]
        if query.get("limit"):
            params["limit"] = query["limit"]
        return f"{self.base_url}/api/remote-jobs?{urlencode(params)}"

    def post_process(self, posting: RawPosting, item: dict) -> RawPosting:
        # Remotive is a remote-only board — every listing on it is remote by
        # definition. That is a property of the board, not of its markup, so it
        # belongs here rather than in selector JSON, which can only point at a
        # field that exists.
        posting.arrangement_hint = posting.arrangement_hint or "Remote"
        return posting
