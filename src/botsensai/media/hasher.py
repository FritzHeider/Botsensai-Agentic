"""Fetch posted media and reduce it to perceptual hashes, on a hard budget.

Downloading images is the only place in the system where a collector reaches for
something measured in megabytes rather than kilobytes, so every part of this is
written as a spending decision:

* **Thumbnails, not originals.** A perceptual hash is scale-invariant by
  construction, so the small variant answers the same question. Measured against
  real X media on 2026-08-04: `name=orig` returned a 6590x4690 image — 31
  megapixels, past the decoder's budget and pointless to move over the wire —
  where `name=small` was 688px and 14 KB. The two variants are not byte-scaled
  copies of one another and their hashes land 4-6 bits apart, comfortably inside
  the clustering threshold but not identical; nothing here should assume they
  are. 4chan's thumbnail is the clearer win: 250px against a multi-megabyte
  original.
* **A byte cap enforced while streaming**, not after. `content-length` is a hint
  a hostile host can lie about; the reader stops at the cap regardless.
* **A per-sweep image count and wall-clock deadline.** Media hashing must never
  be the reason a sweep misses its window.

The cache is where the correctness argument sits. A URL that decoded, a URL that
is not an image, and a URL that 404s are all permanent answers and are cached. A
timeout or a transport error is *not*: caching one writes "this post has no
media" into the store forever on the strength of one bad second, and every
downstream reading of `derivative_remix_depth` inherits it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Sequence

import httpx
from cachetools import LRUCache

from botsensai.config import MediaHashSettings, Settings, get_settings
from botsensai.media.imagecodec import UnsupportedImage, decode_luma
from botsensai.media.phash import PERCEPTUAL_PREFIX, perceptual_hash, perceptual_label
from botsensai.models import SocialPost
from botsensai.util.logging import get_logger
from botsensai.util.ratelimit import Pacer

log = get_logger(__name__)

__all__ = ["MediaHasher", "thumbnail_url"]

#: Cached sentinel for "fetched, and it will never hash" — as opposed to a
#: transport failure, which is not cached at all.
_UNHASHABLE = "unhashable"

_TWIMG_HOSTS = ("pbs.twimg.com", "ton.twimg.com")
_FOURCHAN_MEDIA = re.compile(r"^(https?://i\.4cdn\.org/\w+/\d+)(\.(?:jpg|jpeg|png))$", re.I)


def thumbnail_url(url: str) -> str:
    """Rewrite a media URL to the smallest variant the host publishes."""
    if any(host in url for host in _TWIMG_HOSTS):
        base = url.split("?", 1)[0]
        # `name=small` is X's own 680px variant; `format` must be restated
        # because the query string is being replaced wholesale.
        suffix = base.rsplit(".", 1)[-1].lower()
        if suffix in ("jpg", "jpeg", "png"):
            return f"{base.rsplit('.', 1)[0]}?format={suffix}&name=small"
        return url
    match = _FOURCHAN_MEDIA.match(url)
    if match:
        # 4chan's thumbnail is the same name with an `s` and always a JPEG.
        return f"{match.group(1)}s.jpg"
    return url


class MediaHasher:
    """Turns `SocialPost.media_urls` into `SocialPost.media_hashes`."""

    surface = "media"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        fetcher: Callable[[str], Awaitable[bytes]] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.config: MediaHashSettings = self.settings.media_hash
        self._cache: LRUCache[str, str] = LRUCache(maxsize=self.config.cache_entries)
        self._fetcher = fetcher
        self._client: httpx.AsyncClient | None = None
        self.stats: dict[str, int] = {
            "fetched": 0,
            "cache_hits": 0,
            "hashed": 0,
            "unhashable": 0,
            "failed": 0,
            "skipped_budget": 0,
        }

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- public entry point ------------------------------------------------- #

    async def hash_posts(self, posts: Sequence[SocialPost]) -> int:
        """Populate perceptual hashes in place. Returns how many were added."""
        if not self.config.enabled:
            return 0
        deadline = time.monotonic() + self.config.deadline_seconds
        budget = self.config.max_images_per_sweep
        added = 0
        for post in posts:
            if budget <= 0 or time.monotonic() >= deadline:
                self.stats["skipped_budget"] += len(self._pending(post))
                continue
            spent, gained = await self._hash_one_post(post, budget, deadline)
            budget -= spent
            added += gained
        if added or self.stats["failed"]:
            log.info("media.hashed", added=added, **self.stats)
        return added

    def _pending(self, post: SocialPost) -> list[str]:
        """URLs on this post that still need a perceptual hash."""
        if any(h.startswith(PERCEPTUAL_PREFIX) for h in post.media_hashes):
            return []
        return list(post.media_urls)[: self.config.max_images_per_post]

    async def _hash_one_post(
        self, post: SocialPost, budget: int, deadline: float
    ) -> tuple[int, int]:
        spent = 0
        gained = 0
        for url in self._pending(post):
            if spent >= budget or time.monotonic() >= deadline:
                self.stats["skipped_budget"] += 1
                break
            spent += 1
            digest = await self.hash_url(url)
            if digest is None:
                continue
            label = perceptual_label(digest)
            if label not in post.media_hashes:
                post.media_hashes.append(label)
                gained += 1
        return spent, gained

    async def hash_url(self, url: str) -> str | None:
        """Perceptual hash for one media URL, or None if it cannot be produced."""
        target = thumbnail_url(url) if self.config.prefer_thumbnails else url
        cached = self._cache.get(target)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return None if cached == _UNHASHABLE else cached

        payload = await self._fetch(target)
        if payload is None:
            return None
        digest = self._hash_bytes(target, payload)
        if digest is None:
            self._cache[target] = _UNHASHABLE
            self.stats["unhashable"] += 1
            return None
        self._cache[target] = digest
        self.stats["hashed"] += 1
        return digest

    def _hash_bytes(self, url: str, payload: bytes) -> str | None:
        try:
            luma = decode_luma(payload, max_pixels=self.config.max_pixels)
            return perceptual_hash(luma)
        except (UnsupportedImage, ValueError) as exc:
            log.debug("media.undecodable", url=url, error=str(exc))
            return None

    # -- transport ---------------------------------------------------------- #

    async def _fetch(self, url: str) -> bytes | None:
        """Bytes for `url`, or None. Permanent refusals are cached; failures are not."""
        try:
            self.stats["fetched"] += 1
            if self._fetcher is not None:
                return await self._fetcher(url)
            return await self._stream(url)
        except _PermanentlyUnfetchable as exc:
            log.debug("media.refused", url=url, error=str(exc))
            self._cache[url] = _UNHASHABLE
            self.stats["unhashable"] += 1
            return None
        except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
            # Deliberately not cached: this says nothing about the image.
            log.debug("media.transport_error", url=url, error=str(exc))
            self.stats["failed"] += 1
            return None

    async def _stream(self, url: str) -> bytes:
        pacer = Pacer.get(self.surface, self.config.requests_per_minute, self.config.max_concurrency)
        await pacer.bucket.acquire()
        async with pacer.semaphore:
            client = self._ensure_client()
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise _PermanentlyUnfetchable(f"HTTP {response.status_code}")
                return await self._read_capped(response)

    async def _read_capped(self, response: httpx.Response) -> bytes:
        """Read at most `max_bytes`, stopping the transfer rather than truncating.

        A truncated image is worse than none: PNG would fail to inflate but a
        JPEG can decode its first rows and hash to something that looks like a
        real answer for a picture nobody ever saw.
        """
        cap = self.config.max_bytes
        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            buffer += chunk
            if len(buffer) > cap:
                raise _PermanentlyUnfetchable(f"body exceeds {cap} bytes")
        if not buffer:
            # A 2xx with an empty body is a failure, not an absence.
            raise _PermanentlyUnfetchable("empty body")
        return bytes(buffer)

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.timeout_seconds),
                follow_redirects=True,
                headers={"accept": "image/*,*/*;q=0.8"},
            )
        return self._client


class _PermanentlyUnfetchable(Exception):
    """The URL will not yield an image on any retry."""
