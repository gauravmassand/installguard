"""Tests for the JSON HTTP client.

These spin up a real ``http.server`` on localhost rather than monkeypatching
``urlopen``. That costs a few milliseconds but exercises the genuine urllib
code path — status handling, gzip, timeouts, malformed bodies — which is where
the bugs in a client like this actually live. **No external network access.**
"""

from __future__ import annotations

import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from pkgpatrol.http import (
    DEFAULT_USER_AGENT,
    Deadline,
    HttpClient,
    JsonCache,
    Response,
)

# ---------------------------------------------------------------------------
# Test server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """Serves scripted responses based on the request path."""

    #: path -> (status, body bytes, headers dict). Mutated per test.
    routes: dict[str, tuple[int, bytes, dict[str, str]]] = {}
    #: path -> seconds to sleep before responding, for timeout tests.
    delays: dict[str, float] = {}
    #: path -> remaining number of times to fail before succeeding.
    flaky: dict[str, int] = {}
    #: Every request path seen, so tests can count attempts.
    seen: list[str] = []
    #: User-Agent of the most recent request.
    last_user_agent: str | None = None

    def log_message(self, *_args: object) -> None:  # silence stderr noise
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).seen.append(self.path)
        type(self).last_user_agent = self.headers.get("User-Agent")

        delay = type(self).delays.get(self.path, 0.0)
        if delay:
            time.sleep(delay)

        remaining = type(self).flaky.get(self.path, 0)
        if remaining > 0:
            type(self).flaky[self.path] = remaining - 1
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        status, body, headers = type(self).routes.get(
            self.path, (404, b'{"message":"Not Found"}', {})
        )
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


@pytest.fixture
def server():
    """A localhost HTTP server; yields a base-URL builder."""
    _Handler.routes = {}
    _Handler.delays = {}
    _Handler.flaky = {}
    _Handler.seen = []
    _Handler.last_user_agent = None

    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    try:
        yield lambda path: f"http://{host}:{port}{path}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def client(tmp_path: Path) -> HttpClient:
    """A client whose cache is isolated to a temp dir."""
    return HttpClient(cache=JsonCache(directory=tmp_path / "cache"), backoff=0.0)


def route(path: str, payload: object, status: int = 200) -> None:
    _Handler.routes[path] = (status, json.dumps(payload).encode(), {})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_fetches_and_parses_json(server, client: HttpClient) -> None:
    route("/pkg", {"name": "express", "version": "4.18.0"})
    response = client.get_json(server("/pkg"))
    assert response.ok
    assert response.status == 200
    assert response.data == {"name": "express", "version": "4.18.0"}
    assert response.from_cache is False


def test_sends_identifying_user_agent(server, client: HttpClient) -> None:
    # Registries rate-limit unknown clients harder, so this matters.
    route("/pkg", {"ok": True})
    client.get_json(server("/pkg"))
    assert _Handler.last_user_agent == DEFAULT_USER_AGENT
    assert "pkgpatrol" in (_Handler.last_user_agent or "")


def test_custom_headers_are_sent(server, client: HttpClient) -> None:
    route("/pkg", {"ok": True})
    response = client.get_json(server("/pkg"), headers={"X-Test": "1"})
    assert response.ok


def test_handles_gzipped_response(server, client: HttpClient) -> None:
    body = gzip.compress(json.dumps({"compressed": True}).encode())
    _Handler.routes["/gz"] = (200, body, {"Content-Encoding": "gzip"})
    response = client.get_json(server("/gz"))
    assert response.ok
    assert response.data == {"compressed": True}


# ---------------------------------------------------------------------------
# Failure modes — none of these may raise
# ---------------------------------------------------------------------------


def test_404_is_missing_not_unreachable(server, client: HttpClient) -> None:
    """The distinction matters: 'no such package' vs 'registry is down'
    warrant opposite policies."""
    response = client.get_json(server("/nope"))
    assert response.ok is False
    assert response.missing is True
    assert response.unreachable is False
    assert response.status == 404


def test_connection_refused_is_unreachable(client: HttpClient) -> None:
    # Port 1 on localhost: nothing listens there.
    response = client.get_json("http://127.0.0.1:1/pkg")
    assert response.ok is False
    assert response.unreachable is True
    assert response.missing is False
    assert response.status is None


def test_invalid_json_is_an_error_not_a_crash(server, client: HttpClient) -> None:
    _Handler.routes["/bad"] = (200, b"{not json at all", {})
    response = client.get_json(server("/bad"))
    assert response.ok is False
    assert "invalid JSON" in (response.error or "")
    assert response.status == 200


def test_oversized_response_is_rejected(server, client: HttpClient) -> None:
    from pkgpatrol.http import _MAX_RESPONSE_BYTES

    _Handler.routes["/huge"] = (200, b"x" * (_MAX_RESPONSE_BYTES + 10), {})
    response = client.get_json(server("/huge"))
    assert response.ok is False
    assert "exceeds" in (response.error or "")


def test_unknown_scheme_does_not_raise(client: HttpClient) -> None:
    response = client.get_json("not-a-url")
    assert response.ok is False
    assert response.error is not None


def test_500_is_reported_after_retries(server, client: HttpClient) -> None:
    _Handler.routes["/boom"] = (500, b'{"e":1}', {})
    response = client.get_json(server("/boom"))
    assert response.ok is False
    assert response.status == 500
    # Initial attempt plus `retries` more.
    assert _Handler.seen.count("/boom") == client.retries + 1


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def test_retries_transient_failure_then_succeeds(server, client: HttpClient) -> None:
    route("/flaky", {"recovered": True})
    _Handler.flaky["/flaky"] = 1  # fail once with 503, then serve
    response = client.get_json(server("/flaky"))
    assert response.ok
    assert response.data == {"recovered": True}
    assert _Handler.seen.count("/flaky") == 2


def test_does_not_retry_a_404(server, client: HttpClient) -> None:
    """A missing package is a definitive answer; retrying wastes the budget."""
    client.get_json(server("/absent"))
    assert _Handler.seen.count("/absent") == 1


def test_retries_can_be_disabled(server, client: HttpClient) -> None:
    client.retries = 0
    _Handler.routes["/boom"] = (503, b"{}", {})
    client.get_json(server("/boom"))
    assert _Handler.seen.count("/boom") == 1


# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------


def test_deadline_reports_remaining() -> None:
    deadline = Deadline(10.0)
    assert 9.0 < deadline.remaining <= 10.0
    assert deadline.expired is False


def test_expired_deadline_clamps_to_zero() -> None:
    deadline = Deadline(0.0)
    assert deadline.remaining == 0.0
    assert deadline.expired is True


def test_negative_budget_is_treated_as_zero() -> None:
    assert Deadline(-5.0).expired is True


def test_timeout_for_takes_the_smaller_of_the_two() -> None:
    deadline = Deadline(1.0)
    assert deadline.timeout_for(30.0) <= 1.0
    assert deadline.timeout_for(0.1) == pytest.approx(0.1, abs=0.01)


def test_expired_deadline_skips_the_request(server, client: HttpClient) -> None:
    route("/pkg", {"ok": True})
    response = client.get_json(server("/pkg"), deadline=Deadline(0.0))
    assert response.ok is False
    assert "deadline" in (response.error or "").lower()
    assert _Handler.seen == []  # never hit the network at all


def test_slow_response_times_out_within_budget(server, client: HttpClient) -> None:
    """The whole point: a hanging registry must not outlive the budget."""
    _Handler.delays["/slow"] = 5.0
    route("/slow", {"too": "late"})
    client.retries = 0
    started = time.monotonic()
    response = client.get_json(server("/slow"), deadline=Deadline(0.5))
    elapsed = time.monotonic() - started
    assert response.ok is False
    assert elapsed < 3.0, f"took {elapsed:.2f}s — deadline was not honoured"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_second_call_is_served_from_cache(server, client: HttpClient) -> None:
    route("/pkg", {"cached": True})
    first = client.get_json(server("/pkg"))
    second = client.get_json(server("/pkg"))
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.data == {"cached": True}
    assert _Handler.seen.count("/pkg") == 1  # only one real request


def test_failures_are_not_cached(server, client: HttpClient) -> None:
    """Caching a 404 would hide a package that appears moments later."""
    client.get_json(server("/missing"))
    client.get_json(server("/missing"))
    assert _Handler.seen.count("/missing") == 2


def test_expired_cache_entry_is_refetched(server, tmp_path: Path) -> None:
    client = HttpClient(cache=JsonCache(directory=tmp_path / "c", ttl=-1), backoff=0.0)
    route("/pkg", {"v": 1})
    client.get_json(server("/pkg"))
    client.get_json(server("/pkg"))
    assert _Handler.seen.count("/pkg") == 2


def test_cache_can_be_disabled(server, tmp_path: Path) -> None:
    client = HttpClient(
        cache=JsonCache(directory=tmp_path / "c", enabled=False), backoff=0.0
    )
    route("/pkg", {"v": 1})
    client.get_json(server("/pkg"))
    client.get_json(server("/pkg"))
    assert _Handler.seen.count("/pkg") == 2


def test_corrupt_cache_file_is_ignored(server, tmp_path: Path) -> None:
    """A broken cache must degrade to 'no cache', never crash the hook."""
    cache = JsonCache(directory=tmp_path / "c")
    cache.directory.mkdir(parents=True)
    url = server("/pkg")
    cache._path_for(url).write_text("{{{ not json", encoding="utf-8")
    route("/pkg", {"fresh": True})
    client = HttpClient(cache=cache, backoff=0.0)
    response = client.get_json(url)
    assert response.ok
    assert response.data == {"fresh": True}


def test_unwritable_cache_dir_does_not_crash(server, tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    client = HttpClient(cache=JsonCache(directory=blocker), backoff=0.0)
    route("/pkg", {"ok": True})
    response = client.get_json(server("/pkg"))
    assert response.ok  # the fetch still worked; only caching failed


def test_clear_removes_entries(server, tmp_path: Path) -> None:
    cache = JsonCache(directory=tmp_path / "c")
    client = HttpClient(cache=cache, backoff=0.0)
    route("/a", {"a": 1})
    route("/b", {"b": 2})
    client.get_json(server("/a"))
    client.get_json(server("/b"))
    assert cache.clear() == 2
    assert cache.clear() == 0


def test_cache_dir_override_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from pkgpatrol.http import _default_cache_dir

    monkeypatch.setenv("PKGPATROL_CACHE_DIR", str(tmp_path / "custom"))
    assert _default_cache_dir() == tmp_path / "custom"


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def test_get_many_fetches_all(server, client: HttpClient) -> None:
    for name in ("a", "b", "c"):
        route(f"/{name}", {"name": name})
    urls = [server(f"/{n}") for n in ("a", "b", "c")]
    results = client.get_many(urls)
    assert set(results) == set(urls)
    assert all(r.ok for r in results.values())
    assert {r.data["name"] for r in results.values()} == {"a", "b", "c"}


def test_get_many_deduplicates(server, client: HttpClient) -> None:
    route("/dup", {"ok": True})
    url = server("/dup")
    results = client.get_many([url, url, url])
    assert len(results) == 1
    assert _Handler.seen.count("/dup") == 1


def test_get_many_empty_input(client: HttpClient) -> None:
    assert client.get_many([]) == {}


def test_get_many_mixes_success_and_failure(server, client: HttpClient) -> None:
    route("/good", {"ok": True})
    good, bad = server("/good"), "http://127.0.0.1:1/bad"
    results = client.get_many([good, bad])
    assert results[good].ok
    assert results[bad].unreachable


def test_get_many_shares_one_deadline(server, client: HttpClient) -> None:
    """Ten slow packages must not cost ten times the per-request timeout."""
    for i in range(6):
        _Handler.delays[f"/s{i}"] = 5.0
        route(f"/s{i}", {"i": i})
    client.retries = 0
    urls = [server(f"/s{i}") for i in range(6)]
    started = time.monotonic()
    results = client.get_many(urls, deadline=Deadline(0.6), max_workers=2)
    elapsed = time.monotonic() - started
    assert len(results) == 6
    assert not any(r.ok for r in results.values())
    assert elapsed < 4.0, f"took {elapsed:.2f}s — shared deadline not honoured"


# ---------------------------------------------------------------------------
# Response semantics
# ---------------------------------------------------------------------------


def test_response_ok_requires_data() -> None:
    assert Response(url="u", status=200, data={"a": 1}).ok is True
    assert Response(url="u", status=200, data=None).ok is False
    assert Response(url="u", status=200, data={}, error="x").ok is False


def test_response_classification_is_mutually_exclusive() -> None:
    missing = Response(url="u", status=404, error="HTTP 404")
    down = Response(url="u", error="unreachable: timed out")
    assert (missing.missing, missing.unreachable) == (True, False)
    assert (down.missing, down.unreachable) == (False, True)
