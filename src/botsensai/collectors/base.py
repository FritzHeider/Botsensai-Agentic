"""Collector contract and orchestration.

Every data surface implements `Collector`. The base class handles the parts that
are identical everywhere and easy to get wrong: pacing, timeouts, error
containment, degradation tracking, and recording what a sweep actually produced.

The central design rule is **partial results beat no results**. A sweep where
Instagram is rate-limited and GMGN is behind Cloudflare should still return the
pump.fun and Dexscreener data, flag the two failures, and let the metric layer
lower its confidence accordingly. A collector that raises kills the sweep; a
collector that returns a `CollectionResult` with `degraded=True` does not.
"""

from __future__ import annotations

import abc
import asyncio
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from botsensai.config import CollectorSettings, Settings, get_settings
from botsensai.models import (
    HolderRecord,
    Launch,
    MarketSnapshot,
    SecurityReport,
    SocialAccount,
    SocialPost,
    TokenRef,
    Trade,
    utcnow,
)
from botsensai.util.http import PacedClient
from botsensai.util.logging import get_logger
from botsensai.util.ratelimit import CircuitOpenError

log = get_logger(__name__)


@dataclass
class CollectionResult:
    """Everything one collector produced in one sweep, plus how it went."""

    surface: str
    started_at: datetime
    finished_at: datetime | None = None
    ok: bool = True
    degraded: bool = False
    error: str | None = None

    launches: list[Launch] = field(default_factory=list)
    snapshots: list[MarketSnapshot] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    holders: list[HolderRecord] = field(default_factory=list)
    security: list[SecurityReport] = field(default_factory=list)
    posts: list[SocialPost] = field(default_factory=list)
    accounts: list[SocialAccount] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def record_count(self) -> int:
        return (
            len(self.launches)
            + len(self.snapshots)
            + len(self.trades)
            + len(self.holders)
            + len(self.security)
            + len(self.posts)
            + len(self.accounts)
        )

    @property
    def duration_seconds(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    def merge(self, other: CollectionResult) -> CollectionResult:
        self.launches.extend(other.launches)
        self.snapshots.extend(other.snapshots)
        self.trades.extend(other.trades)
        self.holders.extend(other.holders)
        self.security.extend(other.security)
        self.posts.extend(other.posts)
        self.accounts.extend(other.accounts)
        self.raw.update(other.raw)
        self.degraded = self.degraded or other.degraded
        self.ok = self.ok and other.ok
        if other.error and not self.error:
            self.error = other.error
        return self


class Collector(abc.ABC):  # noqa: B024 — see note below
    """Base class for every data surface.

    Subclasses implement `discover` (find new launches) and/or `enrich` (deepen
    what we know about a specific token). Neither is marked abstract on purpose:
    a pure social collector has nothing to discover and a firehose has nothing to
    enrich, so both default to returning an empty result rather than forcing
    every subclass to stub out a method it will never use.
    """

    #: Stable surface name; must match a key in Settings.collectors.
    name: str = "base"
    #: Human-readable description surfaced in CLI status output.
    description: str = ""
    #: Whether this collector can find previously-unseen tokens.
    can_discover: bool = False
    #: Whether this collector can add detail to a known token.
    can_enrich: bool = False

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.config: CollectorSettings = self.settings.collector(self.name)
        self._client: PacedClient | None = None
        self.consecutive_failures = 0
        self.last_success_at: datetime | None = None

    # -- resources ---------------------------------------------------------- #

    @property
    def client(self) -> PacedClient:
        if self._client is None:
            self._client = PacedClient(
                self.name,
                base_url=self.config.base_url,
                requests_per_minute=self.config.requests_per_minute,
                max_concurrency=self.config.max_concurrency,
                timeout=self.config.timeout_seconds,
                max_retries=self.config.max_retries,
                cache_ttl=self.config.cache_ttl_seconds,
                headers=self.default_headers(),
            )
        return self._client

    def default_headers(self) -> dict[str, str]:
        return {}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- contract ----------------------------------------------------------- #

    async def discover(self, limit: int = 50) -> CollectionResult:
        """Find launches this collector knows about that we may not."""
        return self._empty()

    async def enrich(self, tokens: Sequence[TokenRef]) -> CollectionResult:
        """Add detail for tokens we already know about."""
        return self._empty()

    async def health_check(self) -> bool:
        """Cheap probe used by `botsensai doctor`. Override where a probe exists."""
        return self.config.enabled

    # -- helpers ------------------------------------------------------------ #

    def _empty(self) -> CollectionResult:
        now = utcnow()
        return CollectionResult(surface=self.name, started_at=now, finished_at=now)

    async def run_discover(self, limit: int = 50) -> CollectionResult:
        """`discover` wrapped in error containment, timing, and degradation state."""
        return await self._guard(lambda: self.discover(limit), "discover")

    async def run_enrich(self, tokens: Sequence[TokenRef]) -> CollectionResult:
        return await self._guard(lambda: self.enrich(tokens), "enrich")

    async def _guard(self, fn: Any, op: str) -> CollectionResult:
        started = utcnow()
        result = CollectionResult(surface=self.name, started_at=started)

        if not self.config.enabled:
            result.finished_at = utcnow()
            result.degraded = True
            result.error = "disabled"
            return result

        t0 = time.monotonic()
        try:
            # A collector that hangs is worse than one that fails: it stalls the
            # whole sweep and the decision window for a new token is minutes.
            produced = await asyncio.wait_for(
                fn(), timeout=self.config.timeout_seconds * (self.config.max_retries + 2)
            )
            result.merge(produced)
            result.ok = produced.ok
            result.degraded = produced.degraded
            result.error = produced.error
            self.consecutive_failures = 0
            self.last_success_at = utcnow()
        except TimeoutError:
            result.ok = False
            result.degraded = True
            result.error = f"{op} timed out"
            self.consecutive_failures += 1
            log.warning("collector.timeout", surface=self.name, op=op)
        except CircuitOpenError as exc:
            result.ok = False
            result.degraded = True
            result.error = str(exc)
            log.info("collector.circuit_open", surface=self.name, op=op, retry_after=exc.retry_after)
        except Exception as exc:
            result.ok = False
            result.degraded = True
            result.error = f"{type(exc).__name__}: {exc}"
            self.consecutive_failures += 1
            log.warning("collector.failed", surface=self.name, op=op, error=str(exc))

        result.finished_at = utcnow()
        log.debug(
            "collector.done",
            surface=self.name,
            op=op,
            records=result.record_count,
            seconds=round(time.monotonic() - t0, 2),
            degraded=result.degraded,
        )
        return result


class CollectorRegistry:
    """Holds the active collector set and runs sweeps across it."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._collectors: dict[str, Collector] = {}

    def register(self, collector: Collector) -> Collector:
        self._collectors[collector.name] = collector
        return collector

    def get(self, name: str) -> Collector | None:
        return self._collectors.get(name)

    def all(self) -> list[Collector]:
        return list(self._collectors.values())

    def discoverers(self) -> list[Collector]:
        return [c for c in self._collectors.values() if c.can_discover and c.config.enabled]

    def enrichers(self) -> list[Collector]:
        return [c for c in self._collectors.values() if c.can_enrich and c.config.enabled]

    async def sweep_discover(self, limit: int = 50) -> list[CollectionResult]:
        """Run every discoverer concurrently. Never raises."""
        collectors = self.discoverers()
        if not collectors:
            return []
        return list(
            await asyncio.gather(*(c.run_discover(limit) for c in collectors), return_exceptions=False)
        )

    async def sweep_enrich(self, tokens: Sequence[TokenRef]) -> list[CollectionResult]:
        collectors = self.enrichers()
        if not collectors or not tokens:
            return []
        return list(
            await asyncio.gather(*(c.run_enrich(tokens) for c in collectors), return_exceptions=False)
        )

    async def aclose(self) -> None:
        await asyncio.gather(*(c.aclose() for c in self._collectors.values()), return_exceptions=True)

    @staticmethod
    def combine(results: Sequence[CollectionResult], surface: str = "sweep") -> CollectionResult:
        """Fold many collector results into one, preserving degradation state."""
        started = min((r.started_at for r in results), default=utcnow())
        combined = CollectionResult(surface=surface, started_at=started)
        for r in results:
            combined.merge(r)
        combined.finished_at = utcnow()
        return combined

    def new_run_id(self) -> str:
        return uuid.uuid4().hex[:12]


__all__ = ["CollectionResult", "Collector", "CollectorRegistry"]
