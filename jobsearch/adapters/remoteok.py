from __future__ import annotations

from jobsearch.adapters.base import JsonAdapter, salary_range_text


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

        # RemoteOK uses 0 (or an absent key) to mean "not stated", and states no
        # currency or period of its own — the figures are US dollars per year.
        posting.salary_raw = salary_range_text(item.get("salary_min"),
                                               item.get("salary_max"))
        return posting
