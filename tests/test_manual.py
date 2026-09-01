import json

import httpx
import pytest

from jobsearch import manual


def resolves_to(*ips):
    """Stand in for DNS so the guard can be tested without a network."""
    mapping = {}

    def fake(host, port, *args, **kwargs):
        ip = mapping.get(host, ips[0])
        return [(2, 1, 6, "", (ip, port or 443))]

    fake.mapping = mapping
    return fake


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.10",
                                "169.254.169.254", "172.16.0.1", "::1",
                                "64:ff9b::a9fe:a9fe"])
def test_an_address_off_the_public_internet_is_refused(monkeypatch, ip):
    monkeypatch.setattr(manual.socket, "getaddrinfo", resolves_to(ip))
    with pytest.raises(ValueError, match="non-public"):
        manual.check_target("https://looks-fine.example.com/jobs/1")


def test_a_public_address_passes(monkeypatch):
    monkeypatch.setattr(manual.socket, "getaddrinfo", resolves_to("93.184.216.34"))
    assert manual.check_target("https://example.com/jobs/1") == "https://example.com/jobs/1"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)",
                                 "ftp://example.com/x", "gopher://example.com"])
def test_a_non_http_scheme_is_refused(url):
    with pytest.raises(ValueError, match="scheme"):
        manual.check_target(url)


def test_an_unresolvable_host_is_refused(monkeypatch):
    def boom(*args, **kwargs):
        raise manual.socket.gaierror("nope")
    monkeypatch.setattr(manual.socket, "getaddrinfo", boom)
    with pytest.raises(ValueError, match="resolve"):
        manual.check_target("https://nowhere.invalid/x")


def test_a_redirect_into_private_space_is_refused(monkeypatch):
    dns = resolves_to("93.184.216.34")
    dns.mapping.update({"public.example.com": "93.184.216.34",
                        "internal.example.com": "10.0.0.5"})
    monkeypatch.setattr(manual.socket, "getaddrinfo", dns)

    def handler(request):
        if request.url.host == "public.example.com":
            return httpx.Response(302, headers={"Location": "https://internal.example.com/x"})
        return httpx.Response(200, text="never reached")

    with pytest.raises(ValueError, match="non-public"):
        manual.safe_fetch_url("https://public.example.com/x",
                              transport=httpx.MockTransport(handler))


def test_a_public_redirect_is_followed(monkeypatch):
    monkeypatch.setattr(manual.socket, "getaddrinfo", resolves_to("93.184.216.34"))

    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://example.com/new"})
        return httpx.Response(200, text="<html>ok</html>")

    url, body = manual.safe_fetch_url("https://example.com/old",
                                      transport=httpx.MockTransport(handler))
    assert url.endswith("/new")
    assert "ok" in body


def test_an_endless_redirect_chain_stops(monkeypatch):
    monkeypatch.setattr(manual.socket, "getaddrinfo", resolves_to("93.184.216.34"))

    def handler(request):
        return httpx.Response(302, headers={"Location": "https://example.com/again"})

    with pytest.raises(ValueError, match="redirects"):
        manual.safe_fetch_url("https://example.com/x",
                              transport=httpx.MockTransport(handler))


def test_an_oversized_body_is_refused(monkeypatch):
    monkeypatch.setattr(manual.socket, "getaddrinfo", resolves_to("93.184.216.34"))

    def handler(request):
        return httpx.Response(200, content=b"x" * (manual.MAX_BODY + 10))

    with pytest.raises(ValueError, match="larger"):
        manual.safe_fetch_url("https://example.com/x",
                              transport=httpx.MockTransport(handler))


from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "manual"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_json_ld_gives_a_complete_posting():
    posting = manual.from_jsonld(fixture("jsonld.html"))
    assert posting["title"] == "Senior Backend Engineer"
    assert posting["company"] == "Acme Payments"
    assert "Laravel" in posting["description"]
    assert "<p>" not in posting["description"]      # markup stripped, not shown
    assert posting["location"] == "Berlin, DE"
    assert posting["salary_raw"] == "6000-7500 EUR MONTH"
    assert posting["posted_at"].date().isoformat() == "2026-08-20"


def test_a_page_without_json_ld_yields_nothing_rather_than_a_guess():
    assert manual.from_jsonld(fixture("bare.html")) is None


def test_the_fallback_never_passes_a_page_title_off_as_a_job_title():
    posting = manual.from_meta(fixture("bare.html"))
    assert posting["title"] == "Careers | Acme"
    assert posting["company"] == "Acme Payments"
    assert "backend engineer" in posting["description"]


@pytest.mark.parametrize("url,vendor,slug,job_id", [
    ("https://boards.greenhouse.io/acme/jobs/4512345", "greenhouse", "acme", "4512345"),
    ("https://jobs.lever.co/acme/8a1b2c3d-4e5f-6789-abcd-ef0123456789",
     "lever", "acme", "8a1b2c3d-4e5f-6789-abcd-ef0123456789"),
    ("https://jobs.ashbyhq.com/acme/8a1b2c3d-4e5f-6789-abcd-ef0123456789",
     "ashby", "acme", "8a1b2c3d-4e5f-6789-abcd-ef0123456789"),
])
def test_an_ats_url_is_recognised(url, vendor, slug, job_id):
    assert manual.ats_target(url) == (vendor, slug, job_id)


def test_an_ordinary_careers_url_is_not_an_ats_url():
    assert manual.ats_target("https://acme.com/careers/backend-engineer") is None


def test_extract_prefers_json_ld_and_marks_it_trusted(monkeypatch):
    monkeypatch.setattr(manual, "safe_fetch_url",
                        lambda url, **kw: ("https://acme.com/x", fixture("jsonld.html")))
    result = manual.extract("https://acme.com/x")
    assert result["needs_review"] is False
    assert result["via"] == "jsonld"
    assert result["title"] == "Senior Backend Engineer"


def test_extract_flags_the_fallback_for_review(monkeypatch):
    monkeypatch.setattr(manual, "safe_fetch_url",
                        lambda url, **kw: ("https://acme.com/x", fixture("bare.html")))
    result = manual.extract("https://acme.com/x")
    assert result["needs_review"] is True
    assert result["via"] == "fallback"


def test_ats_slug_becomes_a_company_name():
    _vendor, slug, _job_id = manual.ats_target(
        "https://boards.greenhouse.io/acme-payments/jobs/4512345")
    assert manual._company_from_slug(slug) == "Acme Payments"


def test_from_ats_degrades_to_the_page_when_the_vendor_call_fails(monkeypatch):
    # A pulled Greenhouse posting: the board API 404s (safe_fetch_url raises),
    # but the page itself still parses fine through JSON-LD.
    def fake_fetch(url, **kw):
        if "boards-api.greenhouse.io" in url:
            raise ValueError("404")
        return ("https://boards.greenhouse.io/acme-payments/jobs/4512345", fixture("jsonld.html"))
    monkeypatch.setattr(manual, "safe_fetch_url", fake_fetch)
    result = manual.extract("https://boards.greenhouse.io/acme-payments/jobs/4512345")
    assert result["via"] == "jsonld"
    assert result["title"] == "Senior Backend Engineer"


def test_extract_takes_the_ats_path_and_still_flags_review(monkeypatch):
    payload = json.dumps({
        "id": 4512345,
        "absolute_url": "https://boards.greenhouse.io/acme-payments/jobs/4512345",
        "title": "Senior Backend Engineer",
        "content": "<p>Payments team.</p>",
        "location": {"name": "Berlin, DE"},
        "first_published": "2026-08-20T00:00:00Z",
    })
    monkeypatch.setattr(manual, "safe_fetch_url", lambda url, **kw: (url, payload))
    result = manual.extract("https://boards.greenhouse.io/acme-payments/jobs/4512345")
    assert result["via"] == "ats"
    assert result["company"] == "Acme Payments"
    assert result["needs_review"] is True
