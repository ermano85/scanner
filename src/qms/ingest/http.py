"""Shared HTTP client: bounded concurrency, backoff, and a descriptive User-Agent.

Two of the v1 sources are unofficial endpoints being used for free. Hammering them is
both rude and the fastest way to get blocked, so every request in this codebase goes
through here and inherits the politeness settings.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, TypeVar

import requests

USER_AGENT = "qms-swing-scanner/0.1 (personal research; contact via repo owner)"

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_BASE_S = 1.5
# Deliberately modest. A full backfill is ~12,000 requests against a free, unofficial
# endpoint; six workers behind a 20/second ceiling finishes in roughly a quarter of an
# hour, which is fine for a job that runs once. Turning these up is the fastest way to
# get the source blocked for everyone.
DEFAULT_MAX_WORKERS = 6
DEFAULT_MIN_INTERVAL_S = 0.05

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

T = TypeVar("T")
R = TypeVar("R")


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, permanent: bool = False):
        super().__init__(message)
        self.status = status
        # A permanent failure (404, 410) means "stop asking" — a delisted or bogus
        # symbol. Resume logic uses this to avoid retrying it every single night.
        self.permanent = permanent


@dataclass(frozen=True)
class HttpConfig:
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S
    max_workers: int = DEFAULT_MAX_WORKERS
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S


class _RateLimiter:
    """Process-wide floor on the gap between request starts."""

    def __init__(self, min_interval_s: float):
        self._min_interval = min_interval_s
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self._min_interval
        if wait:
            time.sleep(wait)


class HttpClient:
    def __init__(self, config: HttpConfig | None = None, headers: dict[str, str] | None = None):
        self.config = config or HttpConfig()
        self._limiter = _RateLimiter(self.config.min_interval_s)
        self._local = threading.local()
        self._headers = {"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {})}

    @property
    def _session(self) -> requests.Session:
        # requests.Session is not documented as thread-safe; one per worker thread.
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self._headers)
            self._local.session = session
        return session

    def get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            self._limiter.acquire()
            try:
                response = self._session.get(url, params=params, timeout=self.config.timeout_s)
            except requests.RequestException as exc:
                last_error = exc
            else:
                if response.status_code == requests.codes.ok:
                    return response
                if response.status_code in (404, 410):
                    raise HttpError(
                        f"{response.status_code} for {url}", response.status_code, permanent=True
                    )
                if response.status_code not in RETRY_STATUS:
                    raise HttpError(f"{response.status_code} for {url}", response.status_code)
                last_error = HttpError(
                    f"{response.status_code} for {url}", response.status_code
                )

            if attempt < self.config.max_attempts:
                # Exponential backoff with jitter, so parallel workers that all hit a 429
                # do not retry in lockstep.
                delay = self.config.backoff_base_s**attempt
                time.sleep(delay * (0.5 + random.random()))

        raise HttpError(f"exhausted {self.config.max_attempts} attempts for {url}") from last_error

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response = self.get(url, params)
        try:
            return response.json()
        except ValueError as exc:
            raise HttpError(f"non-JSON response from {url}: {response.text[:200]!r}") from exc

    def map(
        self,
        func: Callable[[T], R],
        items: Iterable[T],
        on_error: Callable[[T, Exception], None] | None = None,
    ) -> Iterator[tuple[T, R]]:
        """Run `func` over `items` with bounded concurrency, yielding as results land.

        Failures are reported through `on_error` and skipped rather than cancelling the
        run — one delisted ticker must not abort a 13,000-symbol backfill.
        """
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            futures = {pool.submit(func, item): item for item in items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    yield item, future.result()
                except Exception as exc:  # noqa: BLE001 — reported, then skipped
                    if on_error is not None:
                        on_error(item, exc)
