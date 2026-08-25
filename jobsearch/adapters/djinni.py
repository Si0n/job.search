from __future__ import annotations

from urllib.parse import urlencode

from jobsearch.adapters.base import HtmlAdapter


class DjinniAdapter(HtmlAdapter):
    name = "djinni"
    base_url = "https://djinni.co"

    def build_url(self, query: dict) -> str:
        params = {"primary_keyword": query.get("keyword", "PHP")}
        if query.get("remote"):
            params["employment"] = "remote"
        if query.get("experience"):
            params["exp_level"] = query["experience"]
        return f"{self.base_url}/jobs/?{urlencode(params)}"
