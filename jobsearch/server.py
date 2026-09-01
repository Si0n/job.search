from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import sys
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jobsearch import dashboard, db, drafts, store, tracker
from jobsearch.profile import load_profile
from jobsearch.models import RawPosting, TRIAGE_STATUSES

VALID_STATUSES = set(TRIAGE_STATUSES)
MAX_NOTE = 2000
STATIC = Path(__file__).parent / "static"


def host_allowed(host_header: str, port: int, lan: bool) -> bool:
    """Whether a request's Host names this server by a literal address.

    This is the DNS-rebinding guard. A rebinding attack works by pointing a
    domain the browser already trusts at this machine's address, so the request
    still arrives carrying the ATTACKER'S DOMAIN in Host. Requiring a literal
    IP is what defeats it, and that property survives widening the accepted
    range from loopback to the private ranges — which is all `lan` does, so a
    phone on the same wifi can reach the dashboard.
    """
    if not host_header:
        return False
    hostname, _, tail = host_header.rpartition(":")
    if not hostname:
        hostname, tail = tail, ""
    hostname = hostname.strip("[]")
    if tail and tail != str(port):
        return False
    if hostname == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return bool(ip.is_loopback or (lan and ip.is_private))


def lan_address() -> str | None:
    """This machine's address on the local network, for the printed URL."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))          # TEST-NET-1, never routed
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def parse_status_request(body: bytes) -> tuple[int, str, str | None]:
    """Pure. Validate a triage request before it reaches the database.

    Every field is checked against a whitelist or a type, because this is the only
    endpoint that writes, and a browser is not a trusted caller even on loopback.
    """
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ValueError("malformed JSON: expected an object")

    raw_id = payload.get("id")
    if isinstance(raw_id, bool):
        # bool subclasses int in Python — int(True) == 1 — so without this,
        # {"id": true} would silently become job id 1.
        raise ValueError(f"invalid job id: {raw_id!r}")
    try:
        job_id = int(raw_id)
    except (TypeError, ValueError):
        raise ValueError(f"invalid job id: {raw_id!r}") from None

    status = payload.get("status")
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")

    note = payload.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise ValueError("invalid note: expected a string")
        if len(note) > MAX_NOTE:
            raise ValueError(f"note too long: {len(note)} > {MAX_NOTE}")

    return job_id, status, note


ROUTES = [
    ("GET",  re.compile(r"^/(?:index\.html)?$"),                "page_dashboard"),
    ("GET",  re.compile(r"^/static/(?P<name>[A-Za-z0-9._-]+)$"), "static_asset"),
    ("GET",  re.compile(r"^/api/jobs$"),                        "api_jobs"),
    ("GET",  re.compile(r"^/api/stats$"),                       "api_stats"),
    ("POST", re.compile(r"^/api/status$"),                      "api_status"),
    ("POST", re.compile(r"^/api/draft-note$"),                  "api_draft_note"),
    ("GET",  re.compile(r"^/applications$"),                       "page_applications"),
    ("GET",  re.compile(r"^/api/stages$"),                         "api_stages"),
    ("GET",  re.compile(r"^/api/applications$"),                   "api_applications"),
    ("GET",  re.compile(r"^/api/applications/(?P<id>\d+)$"),       "api_application"),
    ("GET",  re.compile(r"^/api/activity$"),                       "api_activity"),
    ("GET",  re.compile(r"^/api/tracker-stats$"),                  "api_tracker_stats"),
    ("POST", re.compile(r"^/api/stages$"),                         "post_stage"),
    ("POST", re.compile(r"^/api/applications$"),                   "post_application"),
    ("POST", re.compile(r"^/api/applications/(?P<id>\d+)$"),       "post_application_edit"),
    ("POST", re.compile(r"^/api/applications/(?P<id>\d+)/stage$"), "post_application_stage"),
]


def _make_handler(settings, port, lan: bool = False):
    # Loaded once at startup, not per request: it never changes while the server
    # runs, and a bad profile should fail loudly at boot rather than on a fetch.
    try:
        _profile = load_profile()
        PROFILE, PROFILE_HASH = _profile.data, _profile.hash
    except Exception:
        PROFILE, PROFILE_HASH = None, ""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict | list, content_type="application/json"):
            data = json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_bytes(self, code: int, data: bytes, content_type: str,
                        headers: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _body(self, limit: int = 64 * 1024) -> bytes | None:
            """Read a validated request body, or answer the client and return None.

            CSRF: a cross-origin fetch() sending application/json is preflighted
            and blocked (this server answers no OPTIONS) — but an HTML form with
            enctype="text/plain" is not preflighted, and the classic name/value
            trick makes such a body parse as valid JSON. The whitelist below is
            what actually stops it; a form can only send urlencoded, multipart,
            or text/plain, never application/json.
            """
            content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                self._send(415, {"error": "Content-Type must be application/json"})
                return None
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                self._send(413, {"error": "body too large"})
                return None
            return self.rfile.read(length)

        def _dispatch(self, method: str) -> None:
            # Reads are guarded too, not just writes: the dashboard renders the
            # owner's job list and application drafts, so a rebinding attack that
            # only ever GETs still walks off with all of it.
            if not host_allowed(self.headers.get("Host", ""), port, lan):
                self._send(400, {"error": "invalid host"})
                return
            parsed = urlparse(self.path)
            for verb, pattern, name in ROUTES:
                match = pattern.match(parsed.path)
                if verb == method and match:
                    getattr(self, name)(match, parsed)
                    return
            self._send(404, {"error": "not found"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def page_dashboard(self, match, parsed):
            self._send_bytes(200, (STATIC / "index.html").read_bytes(),
                             "text/html; charset=utf-8")

        def static_asset(self, match, parsed):
            types = {".css": "text/css", ".js": "text/javascript",
                     ".html": "text/html; charset=utf-8"}
            path = STATIC / match.group("name")
            # The regex admits no slash or dot-dot, and resolve() confirms it:
            # a static route is the classic way to read the rest of the disk.
            if path.suffix not in types or not path.resolve().is_relative_to(STATIC.resolve()) \
                    or not path.is_file():
                self._send(404, {"error": "not found"})
                return
            self._send_bytes(200, path.read_bytes(), types[path.suffix])

        def api_jobs(self, match, parsed):
            params = parse_qs(parsed.query)
            min_score = params.get("min_score", [None])[0]
            status_filter = params.get("status", [None])[0]
            conn = db.connect(settings)
            try:
                jobs, postings = dashboard.fetch_rows(
                    conn,
                    min_score=int(min_score) if min_score else None,
                    status_filter=dashboard.parse_job_filter(status_filter),
                )
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._send(400, {"error": "could not load jobs"})
                return
            finally:
                conn.close()
            self._send(200, dashboard.build_view(jobs, postings, PROFILE))

        def api_stats(self, match, parsed):
            conn = db.connect(settings)
            try:
                stats = dashboard.daily_stats(conn)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._send(400, {"error": "could not load stats"})
                return
            finally:
                conn.close()
            self._send(200, stats)

        def api_status(self, match, parsed):
            raw = self._body()
            if raw is None:
                return
            try:
                job_id, status, note = parse_status_request(raw)
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return

            conn = db.connect(settings)
            try:
                result = store.set_status(conn, job_id, status, note)
            except Exception:
                # A syntactically valid but nonexistent job id trips the
                # triage.job_id foreign key. That's a client error, not a
                # server crash — surface it the same way parse_status_request's
                # ValueErrors are surfaced, but log it since a real DB failure
                # should stay visible.
                traceback.print_exc(file=sys.stderr)
                self._send(400, {"error": "could not record status (unknown job id?)"})
                return
            finally:
                conn.close()
            self._send(200, result)

        def api_draft_note(self, match, parsed):
            raw = self._body()
            if raw is None:
                return
            try:
                job_id, note = drafts.parse_note(raw)
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            conn = db.connect(settings)
            try:
                result = drafts.set_note(conn, job_id, note, PROFILE_HASH)
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                self._send(400, {"error": f"could not save note ({type(exc).__name__})"})
                return
            finally:
                conn.close()
            self._send(200, result)

        def _with_conn(self, work, error: str):
            conn = db.connect(settings)
            try:
                payload = work(conn)
            except (ValueError, LookupError) as exc:
                self._send(400, {"error": str(exc)})
                return
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._send(400, {"error": error})
                return
            finally:
                conn.close()
            self._send(200, payload)

        def page_applications(self, match, parsed):
            self._send_bytes(200, (STATIC / "applications.html").read_bytes(),
                             "text/html; charset=utf-8")

        def api_stages(self, match, parsed):
            self._with_conn(tracker.list_stages, "could not load stages")

        def api_applications(self, match, parsed):
            kind = parse_qs(parsed.query).get("kind", [None])[0]
            self._with_conn(
                lambda conn: tracker.build_list(tracker.fetch_list(conn, kind=kind),
                                                datetime.now()),
                "could not load applications")

        def api_application(self, match, parsed):
            application_id = int(match.group("id"))

            def work(conn):
                row, events = tracker.fetch_detail(conn, application_id)
                if row is None:
                    raise LookupError(f"no application {application_id}")
                return tracker.build_detail(row, events, datetime.now())

            self._with_conn(work, "could not load the application")

        def api_activity(self, match, parsed):
            limit = parse_qs(parsed.query).get("limit", ["100"])[0]
            self._with_conn(
                lambda conn: tracker.build_activity(
                    tracker.fetch_activity(conn, int(limit) if limit.isdigit() else 100)),
                "could not load activity")

        def api_tracker_stats(self, match, parsed):
            def work(conn):
                applications, events = tracker.fetch_stats_rows(conn)
                return tracker.build_stats(applications, events, datetime.now())

            self._with_conn(work, "could not load statistics")

        def post_stage(self, match, parsed):
            raw = self._body()
            if raw is None:
                return
            try:
                parsed_stage = tracker.parse_stage_request(raw)
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            self._with_conn(lambda conn: tracker.create_stage(conn, parsed_stage),
                            "could not create the stage")

        def post_application(self, match, parsed):
            raw = self._body()
            if raw is None:
                return
            try:
                request = tracker.parse_application(raw)
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return

            def work(conn):
                job_id = request["job_id"]
                if job_id is None:
                    posting = request["posting"]
                    source = store.source_by_name(conn, "manual")
                    # The URL's digest is the external id, so re-pasting the same
                    # link updates that posting instead of creating a second one.
                    raw_posting = RawPosting(
                        external_id=hashlib.sha256(posting["url"].encode()).hexdigest()[:32],
                        url=posting["url"], title=posting["title"], company=posting["company"],
                        description=posting["description"], location=posting["location"],
                        salary_raw=posting["salary_raw"], posted_at=posting["posted_at"])
                    job_id, _is_new = store.upsert_posting(
                        conn, source, raw_posting, None, settings.rates, datetime.now())
                return tracker.create_application(conn, job_id, request["application"])

            self._with_conn(work, "could not create the application (already tracked?)")

        def post_application_edit(self, match, parsed):
            raw = self._body()
            if raw is None:
                return
            try:
                fields = tracker.parse_edit(raw)
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            application_id = int(match.group("id"))
            self._with_conn(lambda conn: tracker.update_application(conn, application_id, fields),
                            "could not update the application")

        def post_application_stage(self, match, parsed):
            raw = self._body()
            if raw is None:
                return
            try:
                transition = tracker.parse_transition(raw)
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            application_id = int(match.group("id"))
            self._with_conn(lambda conn: tracker.transition(conn, application_id, transition),
                            "could not record the transition")

        def log_message(self, fmt, *args):
            # Default logging writes to stderr on every request, including the
            # polling the page does. Keep the terminal usable.
            pass

    return Handler


def run(settings, host: str | None = None, port: int = 8765,
        open_browser: bool = False, lan: bool = False) -> None:
    host = host or ("0.0.0.0" if lan else "127.0.0.1")
    httpd = ThreadingHTTPServer((host, port), _make_handler(settings, port, lan))
    url = f"http://127.0.0.1:{port}/"
    print(f"jobsearch dashboard on {url}  (ctrl-c to stop)", flush=True)
    if lan:
        address = lan_address()
        if address:
            print(f"  on this network: http://{address}:{port}/", flush=True)
        print("  --lan serves the dashboard to every device on this network, with "
              "no password, and its triage and note endpoints accept writes.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
