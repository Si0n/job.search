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
                                "169.254.169.254", "172.16.0.1", "::1"])
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
