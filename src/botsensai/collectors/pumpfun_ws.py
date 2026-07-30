"""pump.fun new-mint stream, via pumpportal.

The REST discovery path polls `frontend-api-v3/coins?sort=created_timestamp` once
a sweep. At a one-minute cadence that means a token is, on average, thirty
seconds old before Botsensai has ever heard of it — and the decision window for a
new memecoin is measured in the same units. This module removes that delay by
taking the mint events off a socket instead.

`wss://pumpportal.fun/api/data` carries two unauthenticated subscriptions:
`subscribeNewToken` and `subscribeMigration`. Verified live on 2026-07-30, a
create event looks like this — the eight fields past `solAmount` are not in the
published documentation:

    {"signature": "4Lhz…", "mint": "97QU…pump", "traderPublicKey": "HpAR…",
     "txType": "create", "initialBuy": 27534883.660147, "solAmount": 0.790123455,
     "bondingCurveKey": "Cm7t…", "vTokensInBondingCurve": 1045465116.339853,
     "vSolInBondingCurve": 30.790123454, "marketCapSol": 29.451124646,
     "name": "Smooth Like Butter", "symbol": "SLB", "uri": "https://ipfs.io/…",
     "is_mayhem_mode": false, "pool": "pump"}

**There is no timestamp in it.** Not `created_timestamp`, not `blockTime`,
nothing. That single absence shapes everything below:

* `Launch.created_at` is set to the receipt time, which is an *upper bound* on
  the mint time rather than the mint time. The REST path is the only source of
  the authoritative `created_timestamp`, and `Database.upsert_launch` keeps the
  minimum of the two, so the socket's approximation is replaced the moment the
  poller corroborates it and never displaces a truer value.
* Consequently the latency to first observation is only computable for a token
  both paths have seen. `Database.observation_latency()` reports the corroborated
  sample separately from the total for exactly that reason: claiming a median
  latency over rows whose mint time we guessed would be claiming a measurement
  we did not make.

Two further deliberate absences. `initial_supply` is left `None`:
`vTokensInBondingCurve` is a virtual reserve, not a supply figure, and a
plausible-looking wrong number is worse than a missing one. And no USD field is
populated, because the frame is denominated in SOL and this module has no price
oracle — `price_native` is real, `price_usd` stays absent.
"""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from botsensai.config import Settings, get_settings
from botsensai.models import (
    Chain,
    CurveStage,
    Launch,
    Launchpad,
    MarketSnapshot,
    TokenRef,
    utcnow,
)
from botsensai.store.db import Database
from botsensai.util.logging import get_logger

log = get_logger(__name__)

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"

#: Surface name for the `collector_runs` rows this stream writes. Distinct from
#: the polling collector's "pumpfun": they fail independently and for unrelated
#: reasons, and folding them together would hide a dead socket behind a healthy
#: REST path.
WS_SURFACE = "pumpfun_ws"

#: The two subscriptions that need no API key.
SUBSCRIPTIONS = ("subscribeNewToken", "subscribeMigration")

#: Reconnect backoff. A socket that drops once should be back inside a second;
#: a socket that is down because the host is down should not be hammered.
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_FACTOR = 2.0
BACKOFF_MAX_SECONDS = 60.0

#: Enough steps to reach the cap from the base, and no more. See `backoff_delay`.
_MAX_BACKOFF_STEPS = 32

#: A live pump.fun socket delivers several mints a minute. Silence far past that
#: means the connection is a zombie: TCP still open, no frames arriving. Closing
#: and redialling is the only way to find out.
IDLE_TIMEOUT_SECONDS = 180.0


def backoff_delay(attempt: int, jitter: float | None = None) -> float:
    """Exponential backoff with full jitter, capped.

    `attempt` is 0-based. Full jitter (uniform over the whole interval, not a
    small perturbation of the ceiling) is used because every consumer of this
    endpoint reconnects on the same schedule after a server-side restart, and
    the un-jittered version synchronises them into a thundering herd against a
    host that has just proved it is fragile.
    """
    # The exponent is clamped *before* it is raised, not after. A stream that has
    # been redialling a dead host all night reaches attempt counts in the
    # thousands, and `2.0 ** 1024` raises OverflowError rather than returning
    # something the `min` could then discard — turning a reconnect into a crash
    # in precisely the situation reconnecting exists for.
    steps = min(max(0, attempt), _MAX_BACKOFF_STEPS)
    ceiling = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (BACKOFF_FACTOR**steps))
    # Pacing, not cryptography; `random` is the right tool.
    fraction = random.random() if jitter is None else jitter
    return ceiling * min(1.0, max(0.0, fraction))


def _f(value: Any) -> float | None:
    """Coerce to float, tolerating the string-typed numerics this feed emits."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class StreamStats:
    """What one `PumpPortalStream.run` did.

    Kept explicit rather than derived from the store because the interesting
    failure is "connected fine, parsed nothing", which leaves no rows at all.
    """

    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    #: Dials attempted. `connections` counts the ones that handshaked, so a run
    #: with attempts far above connections is a run that spent the night
    #: redialling a host that was not there.
    attempts: int = 0
    connections: int = 0
    failed_connections: int = 0
    messages: int = 0
    launches: int = 0
    migrations: int = 0
    unparsed: int = 0
    acks: int = 0
    errors: list[str] = field(default_factory=list)
    #: Mints seen on the socket that the store had never heard of. This is the
    #: stream's actual yield: a mint the poller had already written is one the
    #: socket did not beat.
    novel_mints: int = 0
    stopped_because: str = "deadline"

    @property
    def duration_seconds(self) -> float:
        return ((self.finished_at or utcnow()) - self.started_at).total_seconds()

    def summary(self) -> dict[str, Any]:
        return {
            "duration_seconds": round(self.duration_seconds, 1),
            "stopped_because": self.stopped_because,
            "attempts": self.attempts,
            "connections": self.connections,
            "failed_connections": self.failed_connections,
            "messages": self.messages,
            "launches": self.launches,
            "novel_mints": self.novel_mints,
            "migrations": self.migrations,
            "unparsed": self.unparsed,
            "errors": self.errors[:5],
        }


class PumpPortalStream:
    """Reads new mints off the pumpportal socket and writes them straight through.

    There is no `Collector` subclass here on purpose. `Collector.discover` is a
    request/response contract with a timeout and a per-sweep result, and this is
    a long-lived subscription with no request and no natural end — forcing it
    into that shape would mean either polling the socket (defeating the point) or
    lying about what `run_discover` returned. It writes to the same tables and
    records the same `collector_runs` rows, so everything downstream, including
    the dashboard's integrity panel, sees it exactly like any other surface.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        db: Database | None = None,
        url: str = PUMPPORTAL_WS,
    ) -> None:
        self.settings = settings or get_settings()
        self.db = db or Database(self.settings.path(self.settings.db_path))
        self.url = url
        #: Injected in tests so backoff can be asserted without waiting for it.
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self.stats = StreamStats()

    # -- parsing ------------------------------------------------------------- #

    def parse_launch(self, payload: dict[str, Any], received_at: datetime) -> Launch | None:
        """A `create` frame to a `Launch`, or None if it is not one.

        `solAmount` on a create event is the SOL the deployer spent buying their
        own token in the same transaction, which is precisely `dev_buy_sol` — a
        field the REST listing does not carry at all. The socket is the cheapest
        source of it in the whole system.
        """
        mint = payload.get("mint")
        if not mint or str(payload.get("txType") or "").lower() != "create":
            return None

        return Launch(
            token=TokenRef(
                chain=Chain.SOLANA,
                mint=str(mint),
                symbol=payload.get("symbol"),
                name=payload.get("name"),
            ),
            launchpad=Launchpad.PUMPFUN,
            deployer=payload.get("traderPublicKey"),
            # Receipt time, not mint time — see the module docstring. Kept as the
            # upper bound it is; `upsert_launch` takes the minimum, so the REST
            # path's authoritative `created_timestamp` wins as soon as it lands.
            created_at=received_at,
            observed_at=received_at,
            metadata_uri=payload.get("uri"),
            dev_buy_sol=_f(payload.get("solAmount")),
            source=WS_SURFACE,
        )

    def parse_snapshot(
        self, payload: dict[str, Any], received_at: datetime, token: TokenRef
    ) -> MarketSnapshot | None:
        """The bonding-curve state carried by a create or migrate frame.

        Worth storing despite being SOL-only: this is the t=0 price, and the
        outcome labeller needs a genuine t=0 to compute a multiple against. A
        price reconstructed from the first snapshot the poller happened to take
        is already a minute of price action late.
        """
        virtual_sol = _f(payload.get("vSolInBondingCurve"))
        virtual_tokens = _f(payload.get("vTokensInBondingCurve"))
        price_native = (
            virtual_sol / virtual_tokens
            if virtual_sol is not None and virtual_tokens is not None and virtual_tokens > 0
            else None
        )

        migrated = str(payload.get("txType") or "").lower() in ("migrate", "migration")
        if price_native is None and not migrated:
            return None

        return MarketSnapshot(
            token=token,
            as_of=received_at,
            observed_at=received_at,
            stage=CurveStage.GRADUATED if migrated else CurveStage.BONDING,
            price_native=price_native,
            # No USD anywhere: the frame is denominated in SOL and this module
            # has no oracle. An unconverted number in a USD column would be
            # wrong by a factor of about 150.
            bonding_curve_progress=1.0 if migrated else None,
            pair_address=payload.get("pool") if migrated else None,
            dex="pumpswap" if migrated else "pumpfun",
            source=WS_SURFACE,
        )

    # -- persistence --------------------------------------------------------- #

    def handle(self, payload: dict[str, Any], received_at: datetime) -> None:
        """Route one decoded frame into the store. Never raises on shape."""
        if payload.get("message") is not None and payload.get("mint") is None:
            self.stats.acks += 1
            return

        mint = payload.get("mint")
        tx_type = str(payload.get("txType") or "").lower()
        if not mint:
            self.stats.unparsed += 1
            return

        token = TokenRef(
            chain=Chain.SOLANA,
            mint=str(mint),
            symbol=payload.get("symbol"),
            name=payload.get("name"),
        )

        if tx_type == "create":
            launch = self.parse_launch(payload, received_at)
            if launch is None:
                self.stats.unparsed += 1
                return
            if self.db.launch(launch.token.key) is None:
                self.stats.novel_mints += 1
            self.db.upsert_launch(launch)
            self.stats.launches += 1
        elif tx_type in ("migrate", "migration"):
            self.stats.migrations += 1
        else:
            # Trade frames need an API key, so anything else here is either a new
            # event type or a reshaped feed. Counted, not swallowed silently.
            self.stats.unparsed += 1
            return

        snapshot = self.parse_snapshot(payload, received_at, token)
        if snapshot is not None:
            self.db.insert_snapshots([snapshot])

    def _record_episode(
        self, run_id: str, started_at: datetime, messages: int, error: str | None
    ) -> None:
        """One `collector_runs` row per connection episode.

        Per episode rather than per run, so a night of reconnects leaves a
        readable sequence instead of one row that says "fine" over eleven
        outages. `ok` is keyed on whether the connection actually delivered
        anything: a socket that handshakes and then sits mute is not healthy, and
        the integrity panel reads the newest row per surface.
        """
        self.db.record_run(
            run_id=run_id,
            surface=WS_SURFACE,
            started_at=started_at,
            finished_at=utcnow(),
            ok=error is None and messages > 0,
            records=messages,
            error=error[:500] if error else None,
        )

    # -- the loop ------------------------------------------------------------ #

    async def run(
        self,
        max_seconds: float | None = None,
        max_messages: int | None = None,
        max_connections: int | None = None,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> StreamStats:
        """Stay subscribed until the budget runs out, reconnecting as needed.

        Returns rather than raises for every network outcome. The caller's
        interest is the corpus, and a stream that dies on a DNS blip has not
        collected anything for the rest of the night.

        `max_connections` bounds connection *attempts*, so an endpoint that
        refuses every dial still terminates. All three limits exist for tests and
        for bounded operational runs; the daemon case passes none of them.
        """
        # Imported here rather than at module scope so that importing this module
        # — which `cli.py` does at command level — cannot fail on an environment
        # where the wheel has not been reinstalled since `websockets` was added.
        import websockets
        from websockets.exceptions import ConnectionClosedOK, WebSocketException

        stats = StreamStats()
        self.stats = stats
        deadline = None if max_seconds is None else stats.started_at.timestamp() + max_seconds
        attempt = 0
        # `None` means "keep going". It cannot be `stats.stopped_because`, whose
        # default is already a terminal reason.
        stop: str | None = None

        def data_budget() -> str | None:
            """Budgets that can be exhausted *while connected*."""
            if max_messages is not None and stats.messages >= max_messages:
                return "max_messages"
            if deadline is not None and utcnow().timestamp() >= deadline:
                return "deadline"
            return None

        def dial_budget() -> str | None:
            """Budgets checked before dialling.

            `max_connections` bounds *attempts*, not successful handshakes.
            Counting handshakes leaves an unreachable host redialling until the
            deadline, and — worse — makes the check fire immediately after a
            successful connect, ending an episode before it has read a single
            frame and recording a healthy socket as mute.
            """
            if max_connections is not None and stats.attempts >= max_connections:
                return "max_connections"
            return data_budget()

        try:
            while stop is None:
                stop = dial_budget()
                if stop is not None:
                    break

                stats.attempts += 1
                episode_started = utcnow()
                run_id = uuid.uuid4().hex
                episode_messages = 0
                episode_error: str | None = None

                try:
                    remaining = (
                        None if deadline is None else max(0.1, deadline - utcnow().timestamp())
                    )
                    async with websockets.connect(
                        self.url,
                        open_timeout=min(20.0, remaining) if remaining else 20.0,
                        ping_interval=20,
                        ping_timeout=20,
                        close_timeout=5,
                    ) as socket:
                        stats.connections += 1
                        for method in SUBSCRIPTIONS:
                            await socket.send(json.dumps({"method": method}))

                        while True:
                            stop = data_budget()
                            if stop is not None:
                                break
                            wait = idle_timeout
                            if deadline is not None:
                                wait = min(wait, max(0.01, deadline - utcnow().timestamp()))
                            raw = await asyncio.wait_for(socket.recv(), timeout=wait)
                            received_at = utcnow()
                            stats.messages += 1
                            episode_messages += 1
                            # A successful frame, not a successful handshake, is
                            # what proves the endpoint is working. Resetting on
                            # connect alone would retry a mute socket at full
                            # speed forever.
                            attempt = 0

                            try:
                                payload = json.loads(raw)
                            except (TypeError, ValueError):
                                stats.unparsed += 1
                                continue
                            if not isinstance(payload, dict):
                                stats.unparsed += 1
                                continue
                            try:
                                self.handle(payload, received_at)
                            except Exception as exc:  # a bad frame is not fatal
                                stats.unparsed += 1
                                stats.errors.append(f"{type(exc).__name__}: {exc}")
                                log.warning("pumpfun_ws.handle_failed", error=str(exc))
                            if on_event is not None:
                                on_event(payload)
                except ConnectionClosedOK:
                    # The peer said goodbye properly (close code 1000). That is a
                    # disconnection, not a fault, and recording it as an error
                    # would mark every clean server restart as a failed surface
                    # on the integrity panel. The reconnect is still visible: it
                    # opens a new episode row.
                    log.debug("pumpfun_ws.closed_cleanly", messages=episode_messages)
                except TimeoutError:
                    # Either the idle watchdog or the deadline. Only the former
                    # is a fault; the latter is the run ending normally.
                    if deadline is not None and utcnow().timestamp() >= deadline:
                        stop = "deadline"
                    else:
                        episode_error = f"no frames for {idle_timeout:.0f}s"
                        stats.errors.append(episode_error)
                        log.warning("pumpfun_ws.idle", seconds=idle_timeout)
                except (WebSocketException, OSError) as exc:
                    episode_error = f"{type(exc).__name__}: {exc}"
                    stats.errors.append(episode_error)
                    if episode_messages == 0:
                        stats.failed_connections += 1
                    log.warning("pumpfun_ws.connection_failed", error=str(exc))
                except (KeyboardInterrupt, asyncio.CancelledError):
                    # `asyncio.run` cancels the task rather than raising
                    # KeyboardInterrupt inside it, so both spellings are caught
                    # here for the same reason they are in `Pipeline.collect`.
                    self._record_episode(run_id, episode_started, episode_messages, "interrupted")
                    stop = "interrupted"
                    break

                self._record_episode(run_id, episode_started, episode_messages, episode_error)

                stop = stop or dial_budget()
                if stop is not None:
                    break

                delay = backoff_delay(attempt)
                attempt += 1
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - utcnow().timestamp()))
                if delay > 0:
                    try:
                        await self._sleep(delay)
                    except (KeyboardInterrupt, asyncio.CancelledError):
                        stop = "interrupted"
                        break
        finally:
            stats.stopped_because = stop or "deadline"
            stats.finished_at = utcnow()
            log.info("pumpfun_ws.run", **stats.summary())
        return stats


__all__ = [
    "BACKOFF_MAX_SECONDS",
    "PUMPPORTAL_WS",
    "SUBSCRIPTIONS",
    "WS_SURFACE",
    "PumpPortalStream",
    "StreamStats",
    "backoff_delay",
]
