from __future__ import annotations

from urllib.parse import urlencode

from jobsearch.adapters.base import HtmlAdapter


class DouAdapter(HtmlAdapter):
    name = "dou"
    base_url = "https://jobs.dou.ua"

    def build_url(self, query: dict) -> str:
        params = {"category": query.get("category", "PHP")}
        if query.get("remote"):
            params["remote"] = ""
        if query.get("experience"):
            params["exp"] = query["experience"]
        return f"{self.base_url}/vacancies/?{urlencode(params)}"
