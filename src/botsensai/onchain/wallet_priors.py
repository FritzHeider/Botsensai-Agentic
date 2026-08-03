"""Prior trading history per wallet, restricted to what was knowable at the time.

Two metrics depend on this and both degrade quietly without it.
`fresh_wallet_ratio` treats a missing prior as *unknown* and drops the wallet
from its numerator while keeping it in the denominator, so an empty index makes
manufactured demand read as clean. `sniper_supply_share` treats a missing prior
as *zero*, pinning every sniper at its 0.4 weight floor. Neither raises; they
just report a comfortable number, which is the worst failure mode available.

The counts are bounded on two axes:

* ``before`` — event time. The trade has to have happened before the token being
  scored existed, or it is not prior history, it is this token's own tape.
* ``observed_before`` — knowledge time. We have to have collected the trade by
  the instant being scored. Every other point-in-time read in the store enforces
  this; the wallet count was the one that did not, and on the current corpus 21%
  of the prior trades it credited were not yet knowable.

That second bound is also what makes the cache sound. Once ``observed_before``
is in the past the answer is frozen: any trade written afterwards necessarily
carries a later ``observed_at`` and is excluded by the bound itself. So a key of
``(wallet, before, observed_before)`` can be memoized across sweeps without ever
going stale. Keying on the wallet alone would return one token's answer for
another token's question.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from botsensai.store.db import Database
from botsensai.util.logging import get_logger

log = get_logger(__name__)

#: Distinct (wallet, before, observed_before) answers held in memory. A sweep
#: scores tens of tokens with a few hundred buyers each, so this covers a full
#: sweep with room to spare while staying trivially small in bytes.
DEFAULT_CAPACITY = 50_000

#: Granularity the knowledge bound is floored to, in seconds.
#:
#: The live path scores each token at its own `utcnow()`, so an unrounded
#: knowledge bound is a distinct cache key every single time and the LRU serves
#: nothing — measured at a 0% hit rate against the real store. Flooring makes
#: the bound shared across a sweep, and 8.4% of wallets in the corpus trade more
#: than one token, which is disproportionately the bot population these metrics
#: are trying to see.
#:
#: Flooring is only sound because it rounds *down*: it can exclude a trade we had
#: in fact just observed, never include one we had not. The bound stays
#: conservative, and the value cached is the same value the query returns because
#: the query is given the floored bound too.
DEFAULT_OBSERVED_BUCKET_SECONDS = 60.0


class WalletPriorIndex:
    """Time-restricted prior-trade counts, batched and memoized.

    Not thread-safe by design: the live path builds contexts on one thread, and
    a lock here would be paid on every lookup to protect against a caller that
    does not exist. Give each thread its own index if that changes.
    """

    def __init__(
        self,
        db: Database,
        capacity: int = DEFAULT_CAPACITY,
        observed_bucket_seconds: float = DEFAULT_OBSERVED_BUCKET_SECONDS,
    ) -> None:
        self.db = db
        self.capacity = max(1, capacity)
        self.observed_bucket_seconds = max(0.0, observed_bucket_seconds)
        self._cache: OrderedDict[tuple[str, float, float | None], int] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.queried = 0
        self.skipped_by_profile = 0

    # -- lookup ------------------------------------------------------------- #

    def priors_for(
        self,
        wallets: Iterable[str],
        before: datetime,
        observed_before: datetime | None = None,
    ) -> dict[str, int]:
        """Prior-trade count per wallet, for every wallet asked about.

        Every requested wallet appears in the result. A wallet with no prior
        trades maps to 0 — which is a claim we can make, having looked — rather
        than being absent, which `fresh_wallet_ratio` reads as "no idea".
        """
        unique = list(dict.fromkeys(w for w in wallets if w))
        if not unique:
            return {}

        before_key = before.timestamp()
        observed_before = self._floor_observed(observed_before)
        observed_key = observed_before.timestamp() if observed_before is not None else None

        out: dict[str, int] = {}
        pending: list[str] = []
        for wallet in unique:
            cached = self._get((wallet, before_key, observed_key))
            if cached is None:
                pending.append(wallet)
            else:
                out[wallet] = cached
                self.hits += 1

        if pending:
            self.misses += len(pending)
            resolved = self._resolve(pending, before, observed_before)
            for wallet, count in resolved.items():
                self._put((wallet, before_key, observed_key), count)
            out.update(resolved)

        return out

    def prior_for(
        self, wallet: str, before: datetime, observed_before: datetime | None = None
    ) -> int:
        return self.priors_for([wallet], before, observed_before).get(wallet, 0)

    def _floor_observed(self, observed_before: datetime | None) -> datetime | None:
        """Round the knowledge bound down to a shared bucket. Never rounds up."""
        if observed_before is None or not self.observed_bucket_seconds:
            return observed_before
        bucket = self.observed_bucket_seconds
        stamp = observed_before.timestamp()
        floored = stamp - (stamp % bucket)
        if floored == stamp:
            return observed_before
        return datetime.fromtimestamp(floored, tz=observed_before.tzinfo or UTC)

    def _resolve(
        self, wallets: Sequence[str], before: datetime, observed_before: datetime | None
    ) -> dict[str, int]:
        """Answer the wallets the cache missed, using the profile table to skip work.

        A wallet whose earliest trade in `wallet_profiles` is already at or after
        `before` cannot have prior trades, so it is answered as 0 without a
        counting query. The reverse shortcut is not available: a wallet that has
        *some* history still needs counting, because how much of it falls before
        the cutoff is the actual question.
        """
        first_seen = self.db.wallet_first_seen(wallets)
        cutoff = before.timestamp()

        counts: dict[str, int] = {}
        needs_count: list[str] = []
        for wallet in wallets:
            seen = first_seen.get(wallet)
            if seen is not None and seen >= cutoff:
                counts[wallet] = 0
                self.skipped_by_profile += 1
            else:
                needs_count.append(wallet)

        if needs_count:
            self.queried += len(needs_count)
            counts.update(self.db.wallet_prior_counts(needs_count, before, observed_before))
        return counts

    # -- cache -------------------------------------------------------------- #

    def _get(self, key: tuple[str, float, float | None]) -> int | None:
        value = self._cache.get(key)
        if value is None:
            return None
        self._cache.move_to_end(key)
        return value

    def _put(self, key: tuple[str, float, float | None], value: int) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self.capacity:
            self._cache.popitem(last=False)

    def clear(self) -> None:
        self._cache.clear()

    # -- maintenance -------------------------------------------------------- #

    def refresh_profiles(self, wallets: Iterable[str] | None = None) -> int:
        """Bring `wallet_profiles` up to date with the trades table."""
        return self.db.refresh_wallet_profiles(wallets)

    def stats(self) -> dict[str, int]:
        return {
            "cached": len(self._cache),
            "hits": self.hits,
            "misses": self.misses,
            "queried": self.queried,
            "skipped_by_profile": self.skipped_by_profile,
        }


__all__ = ["DEFAULT_CAPACITY", "DEFAULT_OBSERVED_BUCKET_SECONDS", "WalletPriorIndex"]
