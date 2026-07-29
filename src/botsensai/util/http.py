"""Paced, cached, retrying HTTP client.

All collector HTTP goes through `PacedClient`. It gives every surface:

* a token bucket sized from config so we stay under published rate limits,
* a circuit breaker so a 429 storm backs off instead of escalating to a ban,
* a short-TTL response cache so several metrics reading the same endpoint in one
  sweep cost one request,
* retry with jitter on transient failures only, never on 4xx other than 429,
* an offline replay mode so tests and backtests never touch the network.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import orjson

from botsensai.util.logging import get_logger
from botsensai.util.ratelimit import CircuitOpenError, Pacer

log = get_logger(__name__)

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, url: str, status: int, body: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.url = url
        self.status = status
        self.body = body


class OfflineError(RuntimeError):
    """Raised in offline mode when a request has no recorded fixture."""


@dataclass
class _CacheEntry:
    expires_at: float
    payload: Any


class ResponseCache:
    """Tiny TTL cache keyed on method+url+params. Bounded to avoid unbounded growth."""

    def __init__(self, max_entries: int = 4096) -> None:
        self._entries: dict[str, _CacheEntry] = {}
        self._max = max_entries

    @staticmethod
    def key(method: str, url: str, params: dict[str, Any] | None) -> str:
        if params:
            items = "&".join(f"{k}={params[k]}" for k in sorted(params))
            return f"{method.upper()} {url}?{items}"
        return f"{method.upper()} {url}"

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            self._entries.pop(key, None)
            return None
        return entry.payload

    def put(self, key: str, payload: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        if len(self._entries) >= self._max:
            # Cheap eviction: drop everything already expired, else drop oldest.
            now = time.monotonic()
            expired = [k for k, v in self._entries.items() if v.expires_at < now]
            for k in expired:
                self._entries.pop(k, None)
            if len(self._entries) >= self._max:
                oldest = min(self._entries, key=lambda k: self._entries[k].expires_at)
                self._entries.pop(oldest, None)
        self._entries[key] = _CacheEntry(time.monotonic() + ttl, payload)

    def clear(self) -> None:
        self._entries.clear()


class PacedClient:
    """One instance per collector surface.

    Use as an async context manager, or share a long-lived instance and call
    `aclose()` at shutdown.
    """

    def __init__(
        self,
        surface: str,
        *,
        base_url: str | None = None,
        requests_per_minute: float = 60.0,
        max_concurrency: int = 4,
        timeout: float = 20.0,
        max_retries: int = 3,
        cache_ttl: float = 5.0,
        headers: dict[str, str] | None = None,
        offline_dir: str | Path | None = None,
        record_dir: str | Path | None = None,
        http2: bool = False,
    ) -> None:
        self.surface = surface
        self.base_url = base_url or ""
        self.timeout = timeout
        self.max_retries = max_retries
        self.cache_ttl = cache_ttl
        self.pacer = Pacer.get(surface, requests_per_minute, max_concurrency)
        self.cache = ResponseCache()
        self.offline_dir = Path(offline_dir) if offline_dir else None
        self.record_dir = Path(record_dir) if record_dir else None
        self._headers = {**DEFAULT_HEADERS, **(headers or {})}
        self._client: httpx.AsyncClient | None = None
        self._http2 = http2
        self.stats: dict[str, int] = {
            "requests": 0,
            "cache_hits": 0,
            "retries": 0,
            "failures": 0,
            "circuit_refusals": 0,
        }

    # -- lifecycle ---------------------------------------------------------- #

    async def __aenter__(self) -> PacedClient:
        self._ensure_client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
                headers=self._headers,
                follow_redirects=True,
                http2=self._http2,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- offline fixtures --------------------------------------------------- #

    @staticmethod
    def _fixture_name(method: str, url: str, params: dict[str, Any] | None) -> str:
        import hashlib

        key = ResponseCache.key(method, url, params)
        digest = hashlib.sha256(key.encode()).hexdigest()[:20]
        slug = "".join(c if c.isalnum() else "_" for c in url.split("//")[-1])[:60]
        return f"{slug}_{digest}.json"

    def _load_fixture(self, method: str, url: str, params: dict[str, Any] | None) -> Any:
        if self.offline_dir is None:
            raise OfflineError("offline mode requested without offline_dir")
        path = self.offline_dir / self._fixture_name(method, url, params)
        if not path.exists():
            raise OfflineError(f"no fixture for {method} {url} at {path}")
        return orjson.loads(path.read_bytes())

    def _record_fixture(self, method: str, url: str, params: dict[str, Any] | None, payload: Any) -> None:
        if self.record_dir is None:
            return
        self.record_dir.mkdir(parents=True, exist_ok=True)
        path = self.record_dir / self._fixture_name(method, url, params)
        path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))

    # -- core request ------------------------------------------------------- #

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        cache_ttl: float | None = None,
        expect_json: bool = True,
    ) -> Any:
        cache_key = ResponseCache.key(method, url, params)
        ttl = self.cache_ttl if cache_ttl is None else cache_ttl

        if method.upper() == "GET":
            cached = self.cache.get(cache_key)
            if cached is not None:
                self.stats["cache_hits"] += 1
                return cached

        if self.offline_dir is not None:
            payload = self._load_fixture(method, url, params)
            self.cache.put(cache_key, payload, ttl)
            return payload

        try:
            self.pacer.breaker.check()
        except CircuitOpenError:
            self.stats["circuit_refusals"] += 1
            raise

        client = self._ensure_client()
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            await self.pacer.bucket.acquire()
            async with self.pacer.semaphore:
                try:
                    self.stats["requests"] += 1
                    response = await client.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers=headers,
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                    self.pacer.breaker.record_failure()
                    log.debug(
                        "http.transport_error",
                        surface=self.surface,
                        url=url,
                        attempt=attempt,
                        error=str(exc),
                    )
                else:
                    if response.status_code < 400:
                        self.pacer.breaker.record_success()
                        payload = self._parse(response, expect_json)
                        if method.upper() == "GET":
                            self.cache.put(cache_key, payload, ttl)
                        self._record_fixture(method, url, params, payload)
                        return payload

                    if response.status_code in RETRYABLE_STATUS:
                        self.pacer.breaker.record_failure()
                        last_exc = HttpError(url, response.status_code, response.text)
                        retry_after = self._retry_after(response)
                        log.debug(
                            "http.retryable_status",
                            surface=self.surface,
                            url=url,
                            status=response.status_code,
                            attempt=attempt,
                            retry_after=retry_after,
                        )
                        if retry_after and attempt < self.max_retries:
                            self.stats["retries"] += 1
                            await asyncio.sleep(retry_after)
                            continue
                    else:
                        # Hard client error. Retrying will not help and may look
                        # like an attack; fail immediately without tripping the
                        # breaker, since the endpoint itself is healthy.
                        self.stats["failures"] += 1
                        raise HttpError(url, response.status_code, response.text)

            if attempt < self.max_retries:
                self.stats["retries"] += 1
                backoff = min(30.0, (2**attempt) * 0.5) * (0.6 + random.random() * 0.8)
                await asyncio.sleep(backoff)

        self.stats["failures"] += 1
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if not value:
            return None
        try:
            return min(60.0, float(value))
        except ValueError:
            return 5.0

    @staticmethod
    def _parse(response: httpx.Response, expect_json: bool) -> Any:
        if not expect_json:
            return response.text
        if not response.content:
            return None
        try:
            return orjson.loads(response.content)
        except orjson.JSONDecodeError:
            return response.text

    # -- conveniences ------------------------------------------------------- #

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return await self.request("GET", url, **kwargs)

    async def post_json(self, url: str, json_body: Any, **kwargs: Any) -> Any:
        return await self.request("POST", url, json_body=json_body, **kwargs)

    async def get_text(self, url: str, **kwargs: Any) -> str:
        kwargs["expect_json"] = False
        result = await self.request("GET", url, **kwargs)
        return result if isinstance(result, str) else str(result)


__all__ = [
    "DEFAULT_HEADERS",
    "HttpError",
    "OfflineError",
    "PacedClient",
    "ResponseCache",
]
