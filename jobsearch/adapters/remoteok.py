from __future__ import annotations

from jobsearch.adapters.base import JsonAdapter


class RemoteOkAdapter(JsonAdapter):
    name = "remoteok"
    base_url = "https://remoteok.com"

    def build_url(self, query: dict) -> str:
        tag = query.get("tag")
        return f"{self.base_url}/api?tag={tag}" if tag else f"{self.base_url}/api"

    def post_process(self, posting, item):
        # remoteok.com is a remote-only board by definition — every posting on
        # it is remote. That is a property of the BOARD, not of the feed's
        # markup, so it belongs in code: selectors.json describes where a
        # value sits in a document, and has no way to express a constant.
        posting.arrangement_hint = "remote"

        # salary_min/salary_max are separate numeric fields; salary.parse()
        # takes one free-text string. RemoteOK uses 0 (or an absent key) to
        # mean "not stated" — that must produce no salary_raw at all, not a
        # "$0" string, since a zero salary would fail the owner's filters.
        low = item.get("salary_min") or None
        high = item.get("salary_max") or None
        if low and high:
            posting.salary_raw = f"${low}" if low == high else f"${low} - ${high}"
        elif low or high:
            posting.salary_raw = f"${low or high}"
        return posting
