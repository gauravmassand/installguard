"""Minimal JSON-over-HTTP client built on :mod:`urllib`.

Every registry lookup in pkgpatrol goes through here. Three constraints shape
the design, all of them downstream of running inside a Claude Code hook:

**A hard deadline, not just per-request timeouts.** The hook has a wall-clock
budget (10s by default). Ten packages with a 5s timeout each could stall for
50s and wedge the tool call. :class:`Deadline` is therefore threaded through
every call and shrinks each socket timeout to the time actually left.

**Fail open, loudly.** No method here raises on network trouble. Failures come
back as a :class:`Response` with ``error`` set, so the caller decides policy.
A registry outage must never block an install — it should warn.

**Cache aggressively.** Package metadata barely changes minute to minute, and
an agent installing five packages often re-checks the same ones. Responses go
to a small on-disk JSON cache with a TTL.

Stdlib only: no ``requests``, no ``httpx``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__

__all__ = [
    "Deadline",
    "Response",
    "JsonCache",
    "HttpClient",
    "DEFAULT_USER_AGENT",
]

#: Registries ask for a descriptive User-Agent, and npm rate-limits unknown
#: clients harder. Identifying ourselves is both polite and practical.
DEFAULT_USER_AGENT = (
    f"pkgpatrol/{__version__} (+https://github.com/gauravmassand/pkgpatrol)"
)

#: Per-request socket timeout, seconds. Deliberately short: a slow registry
#: should be abandoned rather than allowed to eat the whole hook budget.
DEFAULT_TIMEOUT = 4.0

#: Total wall-clock budget for a batch of lookups, seconds.
DEFAULT_BUDGET = 8.0

#: How long a cached response stays fresh, seconds (6 hours).
DEFAULT_CACHE_TTL = 6 * 60 * 60

#: Statuses worth retrying. 429 and 5xx are transient; 404 means the package
#: genuinely does not exist, which is an *answer*, not a failure.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 507, 509})

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------


class Deadline:
    """A shared wall-clock budget for a group of requests.

    Construct one per hook invocation and pass it to every call. Each request
    gets ``min(per_request_timeout, remaining)``, so a batch can overrun its
    budget by at most one socket timeout rather than by a multiple of it.

    >>> d = Deadline(0.0)
    >>> d.expired
    True
    """

    __slots__ = ("_deadline", "_budget")

    def __init__(self, budget: float = DEFAULT_BUDGET) -> None:
        self._budget = max(0.0, budget)
        self._deadline = time.monotonic() + self._budget

    @property
    def budget(self) -> float:
        return self._budget

    @property
    def remaining(self) -> float:
        """Seconds left, never negative."""
        return max(0.0, self._deadline - time.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining <= 0.0

    def timeout_for(self, per_request: float) -> float:
        """The socket timeout to use for one request."""
        return min(per_request, self.remaining)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Deadline(remaining={self.remaining:.2f}s of {self._budget:.2f}s)"


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Response:
    """The outcome of one lookup. Never an exception.

    Exactly one of ``data`` or ``error`` is meaningful. A 404 is reported as
    ``status=404`` with ``error`` set but is *not* a transport failure — use
    :attr:`missing` to tell "no such package" from "could not reach registry",
    because they warrant opposite policies.
    """

    url: str
    status: int | None = None
    data: Any | None = None
    error: str | None = None
    from_cache: bool = False
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        """True when we have usable JSON."""
        return self.error is None and self.data is not None

    @property
    def missing(self) -> bool:
        """True when the registry answered definitively that it has no such entry."""
        return self.status == 404

    @property
    def unreachable(self) -> bool:
        """True when we never got an answer — timeout, DNS, connection refused.

        This is the case that must fail *open*: we know nothing, so blocking
        the install would punish the user for our own outage.
        """
        return self.status is None and self.error is not None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _default_cache_dir() -> Path:
    """Platform-appropriate cache directory, honouring XDG and env overrides."""
    override = os.environ.get("PKGPATROL_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
        return Path(base) / "pkgpatrol" / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "pkgpatrol"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "pkgpatrol"


@dataclass
class JsonCache:
    """A flat on-disk cache of JSON responses, keyed by URL hash.

    Intentionally dumb: one file per URL, no index, no eviction beyond TTL.
    Cache failures are swallowed — a broken cache must degrade to "no cache",
    never to a crash, since this runs inside someone's tool call.
    """

    directory: Path = field(default_factory=_default_cache_dir)
    ttl: float = DEFAULT_CACHE_TTL
    enabled: bool = True

    def _path_for(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return self.directory / f"{digest}.json"

    def get(self, url: str) -> Any | None:
        """Return cached JSON for ``url``, or ``None`` if absent or stale."""
        if not self.enabled or self.ttl <= 0:
            return None
        path = self._path_for(url)
        try:
            raw = path.read_text(encoding="utf-8")
            envelope = json.loads(raw)
            if time.time() - float(envelope["stored_at"]) > self.ttl:
                return None
            return envelope["data"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def set(self, url: str, data: Any) -> None:
        """Store ``data`` for ``url``. Silent on failure."""
        if not self.enabled or self.ttl <= 0:
            return
        path = self._path_for(url)
        envelope = {"url": url, "stored_at": time.time(), "data": data}
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash mid-write cannot leave a torn file
            # that later reads would treat as valid JSON.
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(envelope), encoding="utf-8")
            tmp.replace(path)
        except (OSError, TypeError, ValueError):
            return

    def clear(self) -> int:
        """Delete every cached entry. Returns the number of files removed."""
        removed = 0
        try:
            for entry in self.directory.glob("*.json"):
                try:
                    entry.unlink()
                    removed += 1
                except OSError:
                    continue
        except OSError:
            pass
        return removed


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

#: Signature of the low-level fetch, injectable so tests need no network.
Opener = Callable[[urllib.request.Request, float], Any]


def _urlopen(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310


@dataclass
class HttpClient:
    """Fetches JSON with retries, a shared deadline, and an on-disk cache."""

    user_agent: str = DEFAULT_USER_AGENT
    timeout: float = DEFAULT_TIMEOUT
    retries: int = 2
    cache: JsonCache = field(default_factory=JsonCache)
    #: Seeded backoff between retries, seconds. Scaled by attempt number.
    backoff: float = 0.25
    #: Injection point for tests; defaults to :func:`urllib.request.urlopen`.
    opener: Opener = _urlopen

    # -- single request ---------------------------------------------------

    def get_json(
        self,
        url: str,
        deadline: Deadline | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """Fetch ``url`` and parse the body as JSON.

        Returns a :class:`Response` in all cases — network errors, bad JSON,
        and HTTP errors included. Never raises.
        """
        cached = self.cache.get(url)
        if cached is not None:
            return Response(url=url, status=200, data=cached, from_cache=True)

        deadline = deadline or Deadline()
        started = time.monotonic()
        last_error = "no attempt made"
        last_status: int | None = None

        for attempt in range(self.retries + 1):
            if deadline.expired:
                return Response(
                    url=url,
                    status=last_status,
                    error=f"deadline exceeded after {attempt} attempt(s)",
                    elapsed=time.monotonic() - started,
                )

            response = self._attempt(url, deadline, headers)
            if response.ok:
                self.cache.set(url, response.data)
                return Response(
                    url=url,
                    status=response.status,
                    data=response.data,
                    elapsed=time.monotonic() - started,
                )

            last_error = response.error or "unknown error"
            last_status = response.status

            # A definitive answer is not worth retrying.
            if response.status is not None and response.status not in _RETRY_STATUSES:
                break

            if attempt < self.retries:
                pause = min(self.backoff * (2**attempt), deadline.remaining)
                if pause > 0:
                    time.sleep(pause)

        return Response(
            url=url,
            status=last_status,
            error=last_error,
            elapsed=time.monotonic() - started,
        )

    def _attempt(
        self,
        url: str,
        deadline: Deadline,
        headers: Mapping[str, str] | None,
    ) -> Response:
        """One HTTP round trip. Translates every exception into a Response."""
        # Request() itself raises ValueError on a malformed URL, so it has to
        # be inside the guard too — the "never raises" contract covers the
        # whole method, not just the network call.
        try:
            request = urllib.request.Request(  # noqa: S310
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    **(headers or {}),
                },
                method="GET",
            )
        except ValueError as exc:
            return Response(url=url, error=f"invalid URL: {exc}")

        timeout = deadline.timeout_for(self.timeout)
        if timeout <= 0:
            return Response(url=url, error="deadline exceeded before request")

        try:
            with self.opener(request, timeout) as raw:
                status = getattr(raw, "status", None) or raw.getcode()
                body = raw.read(_MAX_RESPONSE_BYTES + 1)
                if len(body) > _MAX_RESPONSE_BYTES:
                    return Response(
                        url=url,
                        status=status,
                        error=f"response exceeds {_MAX_RESPONSE_BYTES} bytes",
                    )
                if raw.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
        except urllib.error.HTTPError as exc:
            # 404 and friends arrive here. Keep the status: "package does not
            # exist" is a finding, not a transport failure.
            return Response(url=url, status=exc.code, error=f"HTTP {exc.code}")
        except urllib.error.URLError as exc:
            return Response(url=url, error=f"unreachable: {exc.reason}")
        except TimeoutError:
            return Response(url=url, error=f"timed out after {timeout:.1f}s")
        except (OSError, gzip.BadGzipFile, ValueError) as exc:
            return Response(url=url, error=f"{type(exc).__name__}: {exc}")

        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            return Response(url=url, status=status, error=f"invalid JSON: {exc}")

        return Response(url=url, status=status, data=data)

    # -- batch ------------------------------------------------------------

    def get_many(
        self,
        urls: Iterable[str],
        deadline: Deadline | None = None,
        max_workers: int = 8,
    ) -> dict[str, Response]:
        """Fetch several URLs concurrently under one shared deadline.

        Threads rather than asyncio: ``urllib`` is blocking, so asyncio would
        need a thread pool underneath anyway, and a pool is far easier to
        reason about inside a hook that must not hang.

        Results are keyed by URL. Duplicates are fetched once.
        """
        unique = list(dict.fromkeys(urls))
        if not unique:
            return {}

        deadline = deadline or Deadline()
        workers = max(1, min(max_workers, len(unique)))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.get_json, url, deadline): url for url in unique}
            results: dict[str, Response] = {}
            for future, url in futures.items():
                try:
                    results[url] = future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    # A worker must never take down the hook.
                    results[url] = Response(
                        url=url, error=f"{type(exc).__name__}: {exc}"
                    )
        return results
