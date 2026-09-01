from __future__ import annotations

import ipaddress
import json
import re
import socket
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from jobsearch import normalize
from jobsearch.adapters.ats import _ashby, _greenhouse, _lever
from jobsearch.adapters.base import TIMEOUT, USER_AGENT, parse_posted_at

MAX_BODY = 2 * 1024 * 1024
MAX_REDIRECTS = 3
ALLOWED_SCHEMES = ("http", "https")
# RFC 6052 well-known prefix for NAT64. is_global does not catch this: Python
# decodes the IPv4-mapped (::ffff:10.0.0.5) and 6to4 (2002:...) embeddings and
# re-checks the embedded address, but not this one — 64:ff9b::169.254.169.254
# reports is_global=True untouched. Reject the whole prefix rather than trying
# to decode and re-check the embedded IPv4: there's no legitimate reason for
# this tool to fetch through a NAT64 translator.
NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")


def check_target(url: str) -> str:
    """Refuse anything that is not a public http(s) endpoint.

    Resolution happens here, before the request, because the hostname is the
    attacker's field: `internal.example.com` can resolve to 10.0.0.5, and the
    dashboard answers on the LAN with no password. `is_global` is what draws
    the line — it already excludes loopback, private, link-local (including the
    169.254.169.254 metadata address), and reserved space.

    Known gap, accepted: this resolves the hostname and httpx resolves it again
    on connect, with nothing pinning the two to the same address, so a
    short-TTL DNS rebind between the two calls can defeat the check. Closing it
    properly means connecting through a custom transport pinned to the address
    validated here, which was ruled out as more than this tool needs to be.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"unsupported url scheme: {url!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"url has no host: {url!r}")

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve {host!r}: {exc}") from None

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        in_nat64 = isinstance(address, ipaddress.IPv6Address) and address in NAT64_PREFIX
        if not address.is_global or address.is_multicast or in_nat64:
            raise ValueError(f"refusing to fetch a non-public address: {host} -> {address}")
    return url


def safe_fetch_url(url: str, *, transport=None) -> tuple[str, str]:
    """Fetch a caller-chosen URL. Returns the final URL and the decoded body.

    Redirects are followed by hand so every hop passes check_target — a public
    first hop redirecting to 10.0.0.5 is the standard way past a guard that only
    checks the URL it was handed.
    """
    for _ in range(MAX_REDIRECTS + 1):
        check_target(url)
        with httpx.Client(follow_redirects=False, timeout=TIMEOUT, transport=transport) as client:
            with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("redirect without a location header")
                    url = str(response.url.join(location))
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                # Streamed and counted rather than .content: the cap has to hold
                # for a server that answers with an endless body.
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BODY:
                        raise ValueError(f"response larger than {MAX_BODY} bytes")
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
            return str(response.url), b"".join(chunks).decode(encoding, errors="replace")
    raise ValueError(f"more than {MAX_REDIRECTS} redirects")


# vendor -> (url pattern, single-posting API, reader). The readers are the ones
# the ATS board adapter already uses; a second copy would drift from the first
# the next time a vendor changes a field name.
ATS_VENDORS = (
    (re.compile(r"^https?://boards\.greenhouse\.io/(?P<slug>[^/?#]+)/jobs/(?P<id>\d+)"),
     "greenhouse", "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}", _greenhouse),
    (re.compile(r"^https?://jobs\.lever\.co/(?P<slug>[^/?#]+)/(?P<id>[0-9a-fA-F-]{36})"),
     "lever", "https://api.lever.co/v0/postings/{slug}/{id}", _lever),
    (re.compile(r"^https?://jobs\.ashbyhq\.com/(?P<slug>[^/?#]+)/(?P<id>[0-9a-fA-F-]{36})"),
     "ashby", "https://api.ashbyhq.com/posting-api/job-board/{slug}", _ashby),
)


def ats_target(url: str) -> tuple[str, str, str] | None:
    for pattern, vendor, _template, _reader in ATS_VENDORS:
        match = pattern.match(url)
        if match:
            return vendor, match.group("slug"), match.group("id")
    return None


def _flatten(item: dict, reader) -> dict:
    """One vendor posting in the shape the rest of this module speaks."""
    raw = reader(item)
    return {
        "title": raw.get("title"),
        "company": None,                       # vendor payloads omit it; the URL slug is not a name
        "description": normalize.description(raw.get("description") or ""),
        "location": raw.get("location"),
        "salary_raw": None,
        "posted_at": parse_posted_at(raw.get("posted_at")),
    }


def from_ats(url: str, *, transport=None) -> dict | None:
    target = ats_target(url)
    if not target:
        return None
    vendor, slug, job_id = target
    pattern, _vendor, template, reader = next(v for v in ATS_VENDORS if v[1] == vendor)
    _final, body = safe_fetch_url(template.format(slug=slug, id=job_id), transport=transport)
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if vendor == "ashby":
        # Ashby publishes a board, not a posting: find the one that was asked for.
        items = [i for i in (payload.get("jobs") or []) if str(i.get("id")) == job_id]
        if not items:
            return None
        return _flatten(items[0], reader)
    return _flatten(payload, reader)


def _nodes(text: str):
    """Every JSON-LD object in one script tag, flattening lists and @graph."""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return []
    pending = parsed if isinstance(parsed, list) else [parsed]
    out = []
    for node in pending:
        if not isinstance(node, dict):
            continue
        out.append(node)
        graph = node.get("@graph")
        if isinstance(graph, list):
            out.extend(n for n in graph if isinstance(n, dict))
    return out


def _address(node) -> str | None:
    if isinstance(node, list):
        node = node[0] if node else None
    if not isinstance(node, dict):
        return None
    address = node.get("address")
    if not isinstance(address, dict):
        return None
    parts = [address.get("addressLocality"), address.get("addressRegion"),
             address.get("addressCountry")]
    parts = [p for p in parts if isinstance(p, str) and p]
    return ", ".join(parts) or None


def _salary(node) -> str | None:
    """The posting's own words about pay, left as text for salary.parse to read."""
    if not isinstance(node, dict):
        return None
    value = node.get("value")
    currency = node.get("currency") or node.get("salaryCurrency") or ""
    if not isinstance(value, dict):
        return None
    low, high = value.get("minValue"), value.get("maxValue")
    unit = value.get("unitText") or ""
    amount = f"{low}-{high}" if low and high else str(low or high or "")
    return " ".join(p for p in (amount, currency, unit) if p) or None


def from_jsonld(html: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        for node in _nodes(tag.string or tag.get_text() or ""):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "JobPosting" not in types:
                continue
            organisation = node.get("hiringOrganization")
            company = (organisation.get("name") if isinstance(organisation, dict)
                       else organisation if isinstance(organisation, str) else None)
            return {
                "title": node.get("title"),
                "company": company,
                "description": normalize.description(node.get("description") or ""),
                "location": _address(node.get("jobLocation")),
                "salary_raw": _salary(node.get("baseSalary")),
                "posted_at": parse_posted_at(node.get("datePosted")),
            }
    return None


def from_meta(html: str) -> dict:
    """Last resort. Everything here is a guess, and the caller says so."""
    soup = BeautifulSoup(html, "lxml")

    def meta(prop: str) -> str | None:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return (tag.get("content") or "").strip() if tag else None

    title = meta("og:title") or (soup.title.get_text().strip() if soup.title else None)
    body = soup.find("main") or soup.body or soup
    return {
        "title": title,
        "company": meta("og:site_name"),
        "description": normalize.description(str(body)),
        "location": None,
        "salary_raw": None,
        "posted_at": None,
    }


def extract(url: str, *, transport=None) -> dict:
    """What the paste-a-URL form gets back. Nothing here writes to the database.

    `needs_review` is a fact about the extraction, not a score: either the page
    told us what this job is, or we guessed from a page title and the owner has
    to look. There is no third state.
    """
    posting = from_ats(url, transport=transport)
    if posting and posting.get("title"):
        return {**posting, "url": url, "via": "ats", "needs_review": posting["company"] is None}

    final_url, html = safe_fetch_url(url, transport=transport)
    posting = from_jsonld(html)
    if posting and posting.get("title") and posting.get("company"):
        return {**posting, "url": final_url, "via": "jsonld", "needs_review": False}

    return {**from_meta(html), "url": final_url, "via": "fallback", "needs_review": True}
