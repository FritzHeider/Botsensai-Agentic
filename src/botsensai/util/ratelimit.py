"""Async rate limiting and circuit breaking.

Every outbound call in Botsensai passes through one of these. The token bucket
paces normal traffic; the circuit breaker stops hammering a surface that has
started refusing us, which is the difference between being rate-limited for a
minute and being IP-banned for a day.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum


class TokenBucket:
    """Classic token bucket, async-safe.

    `rate` tokens are added per second up to `capacity`. `acquire` waits until a
    token is available rather than raising, because for a data collector the
    right response to being over quota is to slow down, not to fail.
    """

    def __init__(self, rate_per_minute: float, capacity: int | None = None) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self.rate = rate_per_minute / 60.0
        self.capacity = float(capacity if capacity is not None else max(1, int(rate_per_minute)))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now

    async def acquire(self, tokens: float = 1.0) -> float:
        """Block until `tokens` are available. Returns seconds actually waited."""
        if tokens > self.capacity:
            raise ValueError(f"requested {tokens} tokens exceeds capacity {self.capacity}")
        waited = 0.0
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                delay = deficit / self.rate
            await asyncio.sleep(delay)
            waited += delay

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a call is refused because the breaker is open."""

    def __init__(self, name: str, retry_after: float) -> None:
        super().__init__(f"circuit '{name}' is open; retry in {retry_after:.1f}s")
        self.name = name
        self.retry_after = retry_after


@dataclass
class CircuitBreaker:
    """Trips after `failure_threshold` failures inside `window_seconds`.

    While open, calls are refused immediately. After `cooldown_seconds` it moves
    to half-open and allows a single probe; success closes it, failure re-opens
    it with an exponentially longer cooldown, capped at `max_cooldown_seconds`.
    """

    name: str
    failure_threshold: int = 5
    window_seconds: float = 60.0
    cooldown_seconds: float = 30.0
    max_cooldown_seconds: float = 900.0

    _failures: deque[float] = field(default_factory=deque, init=False, repr=False)
    _state: BreakerState = field(default=BreakerState.CLOSED, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _current_cooldown: float = field(default=0.0, init=False)
    _consecutive_trips: int = field(default=0, init=False)
    _probe_in_flight: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._current_cooldown = self.cooldown_seconds

    @property
    def state(self) -> BreakerState:
        self._maybe_half_open()
        return self._state

    def _maybe_half_open(self) -> None:
        if self._state is BreakerState.OPEN:
            if time.monotonic() - self._opened_at >= self._current_cooldown:
                self._state = BreakerState.HALF_OPEN
                self._probe_in_flight = False

    def _prune(self) -> None:
        cutoff = time.monotonic() - self.window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    def check(self) -> None:
        """Raise CircuitOpenError if the call should not proceed."""
        self._maybe_half_open()
        if self._state is BreakerState.OPEN:
            remaining = self._current_cooldown - (time.monotonic() - self._opened_at)
            raise CircuitOpenError(self.name, max(0.0, remaining))
        if self._state is BreakerState.HALF_OPEN and self._probe_in_flight:
            raise CircuitOpenError(self.name, 1.0)
        if self._state is BreakerState.HALF_OPEN:
            self._probe_in_flight = True

    def record_success(self) -> None:
        self._failures.clear()
        self._state = BreakerState.CLOSED
        self._probe_in_flight = False
        self._consecutive_trips = 0
        self._current_cooldown = self.cooldown_seconds

    def record_failure(self) -> None:
        now = time.monotonic()
        self._failures.append(now)
        self._prune()
        if self._state is BreakerState.HALF_OPEN:
            self._trip(now)
            return
        if len(self._failures) >= self.failure_threshold:
            self._trip(now)

    def _trip(self, now: float) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = now
        self._probe_in_flight = False
        self._current_cooldown = min(
            self.max_cooldown_seconds,
            self.cooldown_seconds * (2**self._consecutive_trips),
        )
        self._consecutive_trips += 1
        self._failures.clear()

    def reset(self) -> None:
        self._failures.clear()
        self._state = BreakerState.CLOSED
        self._consecutive_trips = 0
        self._current_cooldown = self.cooldown_seconds
        self._probe_in_flight = False


class Pacer:
    """A named bucket + breaker pair, one per collector surface."""

    _registry: dict[str, Pacer] = {}

    def __init__(self, name: str, requests_per_minute: float, max_concurrency: int = 4) -> None:
        self.name = name
        self.bucket = TokenBucket(requests_per_minute)
        self.breaker = CircuitBreaker(name)
        self.semaphore = asyncio.Semaphore(max_concurrency)

    @classmethod
    def get(cls, name: str, requests_per_minute: float, max_concurrency: int = 4) -> Pacer:
        existing = cls._registry.get(name)
        if existing is None:
            existing = cls(name, requests_per_minute, max_concurrency)
            cls._registry[name] = existing
        return existing

    @classmethod
    def clear_registry(cls) -> None:
        cls._registry.clear()


__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "CircuitOpenError",
    "Pacer",
    "TokenBucket",
]
