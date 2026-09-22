from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class HttpError(RuntimeError):
    pass


@dataclass(slots=True)
class HttpResponse:
    body: bytes
    content_type: str
    url: str
    status: int

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8")


class HttpClient:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 30,
        min_interval_seconds: float = 0.34,
        max_response_bytes: int = 20_000_000,
        retries: int = 4,
        deadline: float | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = min_interval_seconds
        self.max_response_bytes = max_response_bytes
        self.retries = retries
        self.deadline = deadline
        self._last_request_at = 0.0

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/json, application/xml;q=0.9, text/xml;q=0.9",
    ) -> HttpResponse:
        query = urllib.parse.urlencode(
            {key: value for key, value in (params or {}).items() if value not in (None, "")},
            doseq=True,
        )
        request_url = f"{url}?{query}" if query else url
        headers = {"Accept": accept, "User-Agent": self.user_agent}

        for attempt in range(self.retries + 1):
            remaining = self.deadline - time.monotonic() if self.deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise TimeoutError("Collection deadline reached")
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_interval_seconds:
                self._pause(self.min_interval_seconds - elapsed)
            request = urllib.request.Request(request_url, headers=headers)
            try:
                remaining = self.deadline - time.monotonic() if self.deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("Collection deadline reached")
                timeout = (
                    min(self.timeout_seconds, remaining) if remaining else self.timeout_seconds
                )
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    self._last_request_at = time.monotonic()
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > self.max_response_bytes:
                        raise HttpError(f"response exceeds {self.max_response_bytes} bytes")
                    chunks = []
                    size = 0
                    while size <= self.max_response_bytes:
                        if self.deadline is not None and time.monotonic() >= self.deadline:
                            raise TimeoutError("Collection deadline reached")
                        chunk = response.read1(min(65536, self.max_response_bytes + 1 - size))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                    body = b"".join(chunks)
                    if len(body) > self.max_response_bytes:
                        raise HttpError(f"response exceeds {self.max_response_bytes} bytes")
                    return HttpResponse(
                        body=body,
                        content_type=response.headers.get_content_type(),
                        url=response.geturl(),
                        status=response.status,
                    )
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.monotonic()
                retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
                if not retryable or attempt >= self.retries:
                    raise HttpError(f"GET {url} failed with HTTP {exc.code}") from exc
                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                self._pause(delay + random.random() * 0.25)
            except urllib.error.URLError as exc:
                self._last_request_at = time.monotonic()
                if attempt >= self.retries:
                    raise HttpError(f"GET {url} failed: {exc.reason}") from exc
                self._pause(2**attempt + random.random() * 0.25)
        raise AssertionError("unreachable")

    def _pause(self, seconds: float) -> None:
        if self.deadline is not None and time.monotonic() + seconds >= self.deadline:
            raise TimeoutError("Retry would exceed collection deadline")
        time.sleep(seconds)

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> tuple[Any, bytes]:
        response = self.get(url, params=params, accept="application/json")
        if response.content_type not in {"application/json", "application/problem+json"}:
            raise HttpError(f"unexpected content type from {url}: {response.content_type}")
        return response.json(), response.body
