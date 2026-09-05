"""Tests for the pump.fun mint stream.

Every test here runs against a `websockets` server bound to 127.0.0.1 on an
ephemeral port. Nothing touches pumpportal: a test that depends on a third
party's uptime reports that third party's weather, not this code's correctness,
and a firehose test that waits for a real mint is a test that hangs at 4am.

The frames the fake server sends are copied verbatim from a live 70-second
capture on 2026-07-30, field for field, including the two subscription
acknowledgements that arrive before any data and the `is_mayhem_mode` key that
appears in no documentation.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
import websockets

from botsensai.collectors.pumpfun_ws import (
    BACKOFF_MAX_SECONDS,
    WS_SURFACE,
    PumpPortalStream,
    backoff_delay,
)
from botsensai.config import Settings
from botsensai.models import CurveStage, Launch, Launchpad, TokenRef, utcnow
from botsensai.store.db import Database

# --- verbatim from the live capture ---------------------------------------- #

ACK_NEW_TOKEN = {"message": "Successfully subscribed to token creation events."}
ACK_MIGRATION = {"message": "Subscribed to 'migration' events."}

CREATE_EVENT = {
    "signature": "4LhzBq8yMuww4fPUuyHccjBUbzVHzS6EqxD3PYELMtbMPGzPsAUXBNbuFWyBmkx97UJT7FcLy6Fb31dyfARNhgHc",
    "mint": "97QUsKyZ1tDbBkWzowCixaZHPfMHz3ME8LQzwa9Upump",
    "traderPublicKey": "HpARLxzXXfsssHUqzKE1mDC7CNF19vwQArdAvKSbwoF3",
    "txType": "create",
    "initialBuy": 27534883.660147,
    "solAmount": 0.790123455,
    "bondingCurveKey": "Cm7tEVsTyyTXtCqsAsEg5SBB3sjoqUbJEdb69vUwXqrj",
    "vTokensInBondingCurve": 1045465116.339853,
    "vSolInBondingCurve": 30.79012345499999,
    "marketCapSol": 29.451124646602683,
    "name": "Smooth Like Butter",
    "symbol": "SLB",
    "uri": "https://ipfs.io/ipfs/bafkreic43ynnj6d2ck6yjzcw5jfal6zerujcnr2z4bjrn2fhbjltzclu34",
    "is_mayhem_mode": False,
    "pool": "pump",
}

SECOND_CREATE = {
    **CREATE_EVENT,
    "signature": "23dbJLFv1ZgpmWxbd1NannDZ8sk62iBcCTFS1XtiBP5TgR8dxbpmWxvkvd1XnbxFWqmbdXX6ohgXTg9bJhRMxRrp",
    "mint": "25Ms6nLvRVkvYeHMvp1vxoqo4KMt4whNmys271HLpump",
    "traderPublicKey": "8tHMVmRPUefys4zzJ6Z84w9XQBq5GA23xaMMihgLKBGe",
    "solAmount": 0.1,
    "name": "check SEARCH!!!",
    "symbol": "pancakes",
}

MIGRATION_EVENT = {
    "signature": "5SY5gJtLXkQwmVADWoDfJqfaC1UzJa918xNcjk9sH8B2ZxUtFsnw91zwWdS52ppyr2L5HQ3J1em4efSmGRhkUM4f",
    "mint": CREATE_EVENT["mint"],
    "txType": "migrate",
    "pool": "pump-amm",
}


# --- fake server ------------------------------------------------------------ #


class FakeFeed:
    """A local pumpportal, with a scripted set of frames per connection.

    Records what the client sent, so the subscription handshake can be asserted
    rather than assumed.
    """

    def __init__(self, scripts: list[list[object]], close_after: bool = True) -> None:
        #: One entry per expected connection; the last is reused if the client
        #: reconnects more often than the script anticipated.
        self.scripts = scripts
        self.close_after = close_after
        self.received: list[list[str]] = []
        self.connections = 0
        self._server = None

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.sockets[0].getsockname()[:2]
        return f"ws://{host}:{port}"

    async def __aenter__(self) -> FakeFeed:
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, socket) -> None:
        index = self.connections
        self.connections += 1
        sent_by_client: list[str] = []
        self.received.append(sent_by_client)

        script = self.scripts[min(index, len(self.scripts) - 1)]

        # Drain the subscription frames the client opens with. They arrive
        # immediately; a short timeout keeps a broken client from hanging a test.
        for _ in range(2):
            try:
                sent_by_client.append(await asyncio.wait_for(socket.recv(), timeout=2.0))
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                break

        try:
            for frame in script:
                await socket.send(frame if isinstance(frame, str) else json.dumps(frame))

            if self.close_after:
                await socket.close()
            else:
                # Stay open and silent so the client's idle watchdog is what fires.
                await asyncio.sleep(30)
        except websockets.exceptions.ConnectionClosed:
            # The client hit its own budget and hung up mid-script. Expected in
            # the max_messages tests; not a server fault worth a traceback.
            pass


def _stream(tmp_path, url: str, name: str = "ws.db") -> PumpPortalStream:
    db = Database(str(tmp_path / name))
    return PumpPortalStream(Settings(), db=db, url=url)


# --- parsing ---------------------------------------------------------------- #


async def test_create_frames_become_launches(tmp_path):
    """The core of the task: a mint on the socket is a launch in the store."""
    async with FakeFeed([[ACK_NEW_TOKEN, ACK_MIGRATION, CREATE_EVENT, SECOND_CREATE]]) as feed:
        stream = _stream(tmp_path, feed.url)
        stats = await stream.run(max_messages=4, max_seconds=10.0)

    assert stats.launches == 2, stats.summary()
    assert stats.acks == 2, "subscription acknowledgements must not be mistaken for data"
    assert stats.unparsed == 0

    stored = stream.db.launch(f"solana:{CREATE_EVENT['mint']}")
    assert stored is not None
    assert stored.token.symbol == "SLB"
    assert stored.token.name == "Smooth Like Butter"
    assert stored.launchpad is Launchpad.PUMPFUN
    assert stored.deployer == CREATE_EVENT["traderPublicKey"]
    assert stored.metadata_uri == CREATE_EVENT["uri"]
    assert stored.source == WS_SURFACE
    # solAmount on a create event is the deployer buying their own token in the
    # same transaction. The REST listing does not carry this at all.
    assert stored.dev_buy_sol == pytest.approx(0.790123455)
    # vTokensInBondingCurve is a virtual reserve, not a supply. Guessing one
    # would be worse than leaving it absent.
    assert stored.initial_supply is None
    stream.db.close()


async def test_the_mint_snapshot_carries_a_real_price_and_no_invented_usd(tmp_path):
    """t=0 price is the point; a SOL figure in a USD column would be 150x wrong."""
    async with FakeFeed([[CREATE_EVENT]]) as feed:
        stream = _stream(tmp_path, feed.url)
        await stream.run(max_messages=1, max_seconds=10.0)

    snapshots = stream.db.snapshots_as_of(f"solana:{CREATE_EVENT['mint']}", utcnow())
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    expected = CREATE_EVENT["vSolInBondingCurve"] / CREATE_EVENT["vTokensInBondingCurve"]
    assert snapshot.price_native == pytest.approx(expected)
    assert snapshot.stage is CurveStage.BONDING
    assert snapshot.price_usd is None
    assert snapshot.market_cap_usd is None
    assert snapshot.liquidity_usd is None
    stream.db.close()


async def test_migration_frames_mark_the_token_graduated(tmp_path):
    """Graduation is the label the outcome work needs; the socket announces it."""
    async with FakeFeed([[CREATE_EVENT, MIGRATION_EVENT]]) as feed:
        stream = _stream(tmp_path, feed.url)
        stats = await stream.run(max_messages=2, max_seconds=10.0)

    assert stats.migrations == 1
    assert stats.launches == 1
    snapshots = stream.db.snapshots_as_of(f"solana:{CREATE_EVENT['mint']}", utcnow())
    graduated = [s for s in snapshots if s.stage is CurveStage.GRADUATED]
    assert len(graduated) == 1
    assert graduated[0].bonding_curve_progress == 1.0
    stream.db.close()


async def test_both_subscriptions_are_sent_on_every_connection(tmp_path):
    """A reconnect that forgets to resubscribe is a socket that goes quiet."""
    async with FakeFeed([[CREATE_EVENT], [SECOND_CREATE]]) as feed:
        stream = _stream(tmp_path, feed.url)
        stream._sleep = lambda _: asyncio.sleep(0)
        await stream.run(max_connections=2, max_seconds=10.0)

    assert feed.connections >= 2
    for handshake in feed.received[:2]:
        methods = {json.loads(frame)["method"] for frame in handshake}
        assert methods == {"subscribeNewToken", "subscribeMigration"}, methods
    stream.db.close()


async def test_junk_frames_are_counted_and_do_not_stop_the_stream(tmp_path):
    """A reshaped feed must degrade to a count, never to a crash or a bad row."""
    frames = [
        "this is not json",
        json.dumps([1, 2, 3]),
        json.dumps({"txType": "buy", "mint": "x" * 20, "solAmount": 1.0}),
        json.dumps({"txType": "create"}),  # no mint
        CREATE_EVENT,
    ]
    async with FakeFeed([frames]) as feed:
        stream = _stream(tmp_path, feed.url)
        stats = await stream.run(max_messages=5, max_seconds=10.0)

    assert stats.messages == 5
    assert stats.launches == 1, "the one good frame after four bad ones must still land"
    assert stats.unparsed == 4
    assert stream.db.counts()["launches"] == 1
    stream.db.close()


# --- reconnection ----------------------------------------------------------- #


def test_backoff_grows_and_is_capped():
    """Reconnect pressure must not be constant, and must not grow without bound."""
    # Jitter pinned to the ceiling so the growth itself is what is asserted.
    ceilings = [backoff_delay(n, jitter=1.0) for n in range(12)]
    assert ceilings[0] == pytest.approx(1.0)
    assert ceilings[1] == pytest.approx(2.0)
    assert ceilings[2] == pytest.approx(4.0)
    assert ceilings == sorted(ceilings), "backoff must be monotonic"
    assert max(ceilings) == pytest.approx(BACKOFF_MAX_SECONDS)
    # Full jitter, not a nudge: the whole interval is reachable, so a fleet of
    # clients restarted together does not redial in lockstep.
    assert backoff_delay(5, jitter=0.0) == 0.0
    assert backoff_delay(5, jitter=0.5) == pytest.approx(backoff_delay(5, jitter=1.0) / 2)


async def test_a_dropped_connection_is_redialled_with_backoff(tmp_path):
    """The server hanging up must cost a reconnect, not the session."""
    async with FakeFeed([[CREATE_EVENT], [SECOND_CREATE], [MIGRATION_EVENT]]) as feed:
        stream = _stream(tmp_path, feed.url)
        delays: list[float] = []

        async def record(seconds: float) -> None:
            delays.append(seconds)

        stream._sleep = record
        stats = await stream.run(max_connections=3, max_seconds=15.0)

    assert stats.connections == 3
    assert stats.launches == 2 and stats.migrations == 1
    assert len(delays) >= 2, "the stream reconnected without waiting at all"
    # Each frame received resets the attempt counter, so a feed that delivers
    # before every close redials promptly instead of escalating.
    assert all(d <= BACKOFF_MAX_SECONDS for d in delays)
    stream.db.close()


async def test_an_unreachable_endpoint_returns_stats_instead_of_raising(tmp_path):
    """A dead host costs the corpus nothing extra; it must not kill the process."""
    # Bind and immediately release a port so the address is almost certainly free.
    async with FakeFeed([[]]) as feed:
        dead = feed.url
    stream = _stream(tmp_path, dead)
    stream._sleep = lambda _: asyncio.sleep(0)

    stats = await stream.run(max_connections=2, max_seconds=10.0)

    assert stats.connections == 0
    assert stats.failed_connections >= 1
    assert stats.errors, "a refused connection must be reported, not swallowed"
    assert stats.launches == 0
    runs = [r for r in stream.db.recent_runs(limit=20) if r["surface"] == WS_SURFACE]
    assert runs and all(r["ok"] is False for r in runs)
    stream.db.close()


async def test_a_silent_socket_is_treated_as_a_fault(tmp_path):
    """TCP open with no frames is a zombie, not a quiet market."""
    async with FakeFeed([[]], close_after=False) as feed:
        stream = _stream(tmp_path, feed.url)
        stream._sleep = lambda _: asyncio.sleep(0)
        stats = await stream.run(max_connections=1, max_seconds=10.0, idle_timeout=0.2)

    assert stats.connections == 1
    assert stats.messages == 0
    assert any("no frames" in e for e in stats.errors), stats.errors
    runs = [r for r in stream.db.recent_runs(limit=20) if r["surface"] == WS_SURFACE]
    assert runs and runs[0]["ok"] is False, (
        "a connected-but-mute socket reported as healthy is exactly the silent "
        "failure the integrity panel exists to catch"
    )
    stream.db.close()


async def test_every_connection_episode_leaves_a_collector_run(tmp_path):
    """A night of reconnects must read back as a sequence, not as one 'fine'."""
    async with FakeFeed([[CREATE_EVENT], [SECOND_CREATE]]) as feed:
        stream = _stream(tmp_path, feed.url)
        stream._sleep = lambda _: asyncio.sleep(0)
        await stream.run(max_connections=2, max_seconds=10.0)

    runs = [r for r in stream.db.recent_runs(limit=20) if r["surface"] == WS_SURFACE]
    assert len(runs) == 2
    assert len({r["run_id"] for r in runs}) == 2, "episodes must not collapse onto one row"
    assert all(r["ok"] for r in runs)
    assert all(r["records"] == 1 for r in runs)
    assert all(r["finished_at"] is not None for r in runs)
    stream.db.close()


# --- latency to first observation ------------------------------------------- #


def _rest_launch(mint: str, created_at, observed_at) -> Launch:
    """What the polling collector writes: a real mint time, a later observation."""
    return Launch(
        token=TokenRef(chain="solana", mint=mint, symbol="SLB"),
        launchpad=Launchpad.PUMPFUN,
        created_at=created_at,
        observed_at=observed_at,
        source="pumpfun",
    )


async def test_the_socket_observation_survives_a_later_rest_sweep(tmp_path):
    """The measurement only works if neither writer clobbers the other's half.

    The socket knows *when we saw it* and not when it was minted; the REST path
    knows when it was minted and saw it late. Keeping the minimum of each is what
    turns two partial records into one true latency.
    """
    async with FakeFeed([[CREATE_EVENT]]) as feed:
        stream = _stream(tmp_path, feed.url)
        await stream.run(max_messages=1, max_seconds=10.0)

    db = stream.db
    key = f"solana:{CREATE_EVENT['mint']}"
    from_socket = db.launch(key)
    assert from_socket is not None
    # No timestamp in the frame, so the socket can only bound the mint time.
    assert from_socket.created_at == from_socket.observed_at

    real_mint_time = from_socket.observed_at - timedelta(seconds=42)
    db.upsert_launch(_rest_launch(CREATE_EVENT["mint"], real_mint_time, utcnow()))

    merged = db.launch(key)
    assert merged is not None
    assert merged.created_at == real_mint_time, "the true mint time must displace the guess"
    assert merged.observed_at == from_socket.observed_at, (
        "a later sweep re-seeing a token must not push its first observation forward"
    )
    assert merged.source == WS_SURFACE, "the discovering surface is not overwritten"

    report = db.observation_latency()
    assert report[WS_SURFACE]["launches"] == 1
    assert report[WS_SURFACE]["measured"] == 1
    assert report[WS_SURFACE]["median_seconds"] == pytest.approx(42.0, abs=1.0)
    db.close()


def test_uncorroborated_rows_are_excluded_rather_than_counted_as_zero(tmp_path):
    """Averaging in the socket's own zeroes would invent a spectacular figure."""
    db = Database(str(tmp_path / "latency.db"))
    now = utcnow()

    # Two mints the socket saw and nothing has corroborated yet.
    for index in range(2):
        db.upsert_launch(
            Launch(
                token=TokenRef(chain="solana", mint=f"ws{index}"),
                created_at=now,
                observed_at=now,
                source=WS_SURFACE,
            )
        )
    # One the socket saw and the poller later dated.
    db.upsert_launch(
        Launch(
            token=TokenRef(chain="solana", mint="ws2"),
            created_at=now,
            observed_at=now,
            source=WS_SURFACE,
        )
    )
    db.upsert_launch(_rest_launch("ws2", now - timedelta(seconds=10), now))
    # And three the poller found first, thirty seconds late each.
    for index in range(3):
        db.upsert_launch(
            _rest_launch(f"rest{index}", now - timedelta(seconds=30), now)
        )

    report = db.observation_latency()

    assert report[WS_SURFACE]["launches"] == 3
    assert report[WS_SURFACE]["measured"] == 1, "zero-latency rows are not a measurement"
    assert report[WS_SURFACE]["median_seconds"] == pytest.approx(10.0, abs=1.0)
    assert report["pumpfun"]["launches"] == 3
    assert report["pumpfun"]["measured"] == 3
    assert report["pumpfun"]["median_seconds"] == pytest.approx(30.0, abs=1.0)
    db.close()


def test_stream_command_is_registered_and_bounded():
    """`stream --minutes` must exist as a command with a bounded window."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "botsensai.cli", "stream", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--minutes" in result.stdout
