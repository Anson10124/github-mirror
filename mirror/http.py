"""Tiny JSON-over-HTTP client with rate-limit aware retries (stdlib only)."""

from __future__ import annotations

import json
import random
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

from .logs import log, redact

USER_AGENT = "github-mirror/1.0"


class HttpError(Exception):
    def __init__(self, method: str, url: str, status: int, body: str):
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        snippet = redact(body.strip().replace("\n", " "))[:300]
        super().__init__(f"{method} {url} -> HTTP {status}: {snippet}")


@dataclass
class Response:
    status: int
    headers: Mapping[str, str]
    data: Any


def _lower_headers(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    return {k.lower(): v for k, v in headers.items()}


def parse_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="?next"?', part)
        if m:
            return m.group(1)
    return None


def backoff(attempt: int) -> float:
    """Exponential backoff with a little jitter: ~2, 4, 8, 16, 32, 64 s (capped at 120)."""
    return min(120.0, 2.0 ** (attempt + 1)) + random.uniform(0, 1)


def retry_delay(status: int, headers: Mapping[str, str], body: str, attempt: int, now: float) -> float | None:
    """How long to wait before retrying, or None if the error is not retryable.

    Handles GitHub primary limits (403/429 with x-ratelimit-remaining: 0),
    GitHub secondary limits (403/429 with Retry-After or "secondary rate limit"
    in the body), GitLab 429s (Retry-After / RateLimit-Reset), and 5xx.
    """
    h = headers
    retry_after = h.get("retry-after")
    if status in (403, 429):
        if retry_after and retry_after.strip().isdigit():
            return float(retry_after) + 1
        remaining = h.get("x-ratelimit-remaining", h.get("ratelimit-remaining"))
        reset = h.get("x-ratelimit-reset", h.get("ratelimit-reset"))
        if remaining == "0" and reset and reset.strip().isdigit():
            return max(1.0, float(reset) - now + 1)
        lowered = body.lower()
        if status == 429 or "secondary rate limit" in lowered or "abuse" in lowered or "rate limit" in lowered:
            return max(60.0, backoff(attempt))
        return None
    if status in (500, 502, 503, 504):
        return backoff(attempt)
    return None


Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(req: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(req, timeout=timeout)


class HttpClient:
    def __init__(
        self,
        base_url: str,
        headers: Mapping[str, str],
        *,
        name: str,
        opener: Opener | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        max_attempts: int = 6,
        max_wait: float = 900.0,
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **headers}
        self.name = name
        self.opener = opener or _default_opener
        self.sleep = sleep
        self.clock = clock
        self.max_attempts = max_attempts
        self.max_wait = max_wait
        self.timeout = timeout

    def _url(self, path_or_url: str, params: Mapping[str, Any] | None) -> str:
        url = path_or_url if path_or_url.startswith(("http://", "https://")) else self.base_url + path_or_url
        if params:
            sep = "&" if "?" in url else "?"
            url += sep + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return url

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        allow_404: bool = False,
    ) -> Response | None:
        url = self._url(path_or_url, params)
        data = None
        headers = dict(self.headers)
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"

        for attempt in range(self.max_attempts):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            status: int
            resp_headers: dict[str, str]
            body: str
            try:
                with self.opener(req, self.timeout) as resp:
                    status = resp.status
                    resp_headers = _lower_headers(resp.headers)
                    body = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                status = e.code
                resp_headers = _lower_headers(e.headers)
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = ""
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
                if attempt + 1 >= self.max_attempts:
                    raise HttpError(method, url, 0, f"network error: {e}") from e
                delay = backoff(attempt)
                log.warning("%s: network error on %s %s (%s); retrying in %.0fs", self.name, method, url, e, delay)
                self.sleep(delay)
                continue

            if 200 <= status < 300:
                parsed: Any = None
                if body.strip():
                    try:
                        parsed = json.loads(body)
                    except ValueError:
                        parsed = body
                return Response(status, resp_headers, parsed)
            if status == 404 and allow_404:
                return None

            delay = retry_delay(status, resp_headers, body, attempt, self.clock())
            if delay is None or attempt + 1 >= self.max_attempts:
                raise HttpError(method, url, status, body)
            delay = min(delay, self.max_wait)
            log.warning("%s: HTTP %s on %s %s (rate limited / server error); retrying in %.0fs",
                        self.name, status, method, url, delay)
            self.sleep(delay)

        raise AssertionError("unreachable")  # pragma: no cover

    def get(self, path: str, **kw: Any) -> Response | None:
        return self.request("GET", path, **kw)

    def paginate(self, path: str, params: Mapping[str, Any] | None = None) -> Iterator[Any]:
        url: str | None = self._url(path, params)
        while url:
            resp = self.request("GET", url)
            assert resp is not None
            items = resp.data or []
            if not isinstance(items, list):
                raise HttpError("GET", url, resp.status, "expected a JSON array")
            yield from items
            url = parse_next_link(resp.headers.get("link"))
