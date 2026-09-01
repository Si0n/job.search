from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from jobsearch.adapters.base import TIMEOUT, USER_AGENT

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
