"""The retry contract of `PacedClient.request`, against a scripted transport.

Every collector's HTTP goes through this one method, and until P0-03 split it
into `_without_network`/`_attempt`/`_backoff` nothing tested it directly — the
retry, backoff and circuit-breaker behaviour was only ever exercised by
accident through collectors that stub the client out entirely.

The distinctions that matter here and are easy to lose in a refactor:

* a retryable status with a `Retry-After` obeys the server verbatim, and one
  without it backs off exponentially — a rate-limited host that tells us to
  wait ten seconds must not be hammered again in half a second,
* a hard 4xx fails on the first response without retrying *and without
  recording a breaker failure*, because the endpoint is healthy and it is our
  request that is wrong; retrying looks like an attack,
* the exception the caller finally sees is the last real one, not a generic.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from botsensai.util.http import HttpError, OfflineError, PacedClient

PACING = {"requests_per_minute": 60000.0, "max_concurrency": 4}


class _Scripted:
    """An `httpx.AsyncClient` stand-in that replays a fixed list of responses.

    A list entry is either `(status, body, headers)` or an exception instance,
    which is raised where the real transport would have raised it.
    """

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.calls: list[str] = []

    async def request(self, method, url, **kwargs):  # noqa: ANN001, ANN202
        self.calls.append(f"{method} {url}")
        item = self.script.pop(0) if self.script else (200, b'{"tail":true}', {})
        if isinstance(item, Exception):
            raise item
        status, body, headers = item
        return httpx.Response(
            status_code=status,
            content=body,
            headers=headers,
            request=httpx.Request(method, "https://scripted.test" + url),
        )

    async def aclose(self) -> None:
        return None


def _client(surface: str, transport: _Scripted, **kwargs) -> PacedClient:
    client = PacedClient(surface=surface, base_url="https://scripted.test", **PACING, **kwargs)
    # Assigned to the private attribute so the lazy `_ensure_client` property
    # never fires and no socket is ever opened.
    client._client = transport  # type: ignore[assignment]
    return client


@pytest.fixture
def naps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record what the retry loop would have slept instead of sleeping."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


def test_success_returns_the_parsed_body() -> None:
    transport = _Scripted((200, b'{"hello":"world"}', {}))
    client = _client("test_ok", transport)
    assert asyncio.run(client.request("GET", "/probe")) == {"hello": "world"}
    assert client.stats["requests"] == 1
    assert client.stats["failures"] == 0


def test_a_body_that_is_not_json_comes_back_as_text() -> None:
    transport = _Scripted((200, b"<html>nope</html>", {}))
    client = _client("test_nonjson", transport)
    assert asyncio.run(client.request("GET", "/probe")) == "<html>nope</html>"


def test_a_hard_4xx_fails_on_the_first_response(naps: list[float]) -> None:
    """No second call, no sleep, and no breaker failure: the endpoint is fine."""
    transport = _Scripted((404, b"missing", {}), (200, b'{"late":true}', {}))
    client = _client("test_hard", transport, max_retries=3)

    with pytest.raises(HttpError) as caught:
        asyncio.run(client.request("GET", "/probe"))

    assert caught.value.status == 404
    assert len(transport.calls) == 1
    assert naps == []
    assert client.stats["retries"] == 0
    assert client.stats["failures"] == 1
    assert not client.pacer.breaker._failures


def test_a_retryable_status_is_retried_and_can_succeed(naps: list[float]) -> None:
    transport = _Scripted((503, b"boom", {}), (200, b'{"ok":1}', {}))
    client = _client("test_retryable", transport, max_retries=2)

    assert asyncio.run(client.request("GET", "/probe")) == {"ok": 1}
    assert len(transport.calls) == 2
    assert len(naps) == 1
    assert client.stats["retries"] == 1


def test_retry_after_is_obeyed_verbatim_instead_of_the_backoff(naps: list[float]) -> None:
    """A host that says "wait 7s" is waited on for 7s, not for our own guess.

    The backoff for attempt 0 is at most 0.4s, so anything in that range means
    the header was ignored — which is how a 429 turns into a ban.
    """
    transport = _Scripted((429, b"slow down", {"retry-after": "7"}), (200, b'{"ok":1}', {}))
    client = _client("test_retry_after", transport, max_retries=2)

    assert asyncio.run(client.request("GET", "/probe")) == {"ok": 1}
    assert naps == [7.0]


def test_retry_after_is_capped_and_a_garbage_value_falls_back(naps: list[float]) -> None:
    transport = _Scripted((429, b"slow", {"retry-after": "9000"}), (200, b"{}", {}))
    assert asyncio.run(_client("test_cap", transport, max_retries=2).request("GET", "/a")) == {}
    assert naps == [60.0]

    naps.clear()
    transport = _Scripted((503, b"slow", {"retry-after": "soon"}), (200, b"{}", {}))
    assert asyncio.run(_client("test_junk", transport, max_retries=2).request("GET", "/a")) == {}
    assert naps == [5.0]


def test_a_zero_retry_after_backs_off_rather_than_retrying_instantly(naps: list[float]) -> None:
    """`retry-after: 0` must not become a zero-delay hot loop against the host."""
    transport = _Scripted((429, b"slow", {"retry-after": "0"}), (200, b"{}", {}))
    client = _client("test_zero", transport, max_retries=2)

    assert asyncio.run(client.request("GET", "/probe")) == {}
    assert naps and naps[0] > 0.0


def test_exhausted_retries_raise_the_last_error(naps: list[float]) -> None:
    transport = _Scripted((500, b"one", {}), (502, b"two", {}), (503, b"three", {}))
    client = _client("test_exhausted", transport, max_retries=2)

    with pytest.raises(HttpError) as caught:
        asyncio.run(client.request("GET", "/probe"))

    assert caught.value.status == 503
    assert len(transport.calls) == 3
    # Two sleeps for three attempts: nothing waits after the final one.
    assert len(naps) == 2
    assert client.stats["failures"] == 1


def test_a_transport_error_is_retried_and_resurfaces_unwrapped(naps: list[float]) -> None:
    transport = _Scripted(httpx.ConnectError("refused"), (200, b'{"ok":1}', {}))
    client = _client("test_transport_ok", transport, max_retries=2)
    assert asyncio.run(client.request("GET", "/probe")) == {"ok": 1}

    transport = _Scripted(
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("last one"),
    )
    client = _client("test_transport_dead", transport, max_retries=2)
    with pytest.raises(httpx.ConnectError, match="last one"):
        asyncio.run(client.request("GET", "/probe"))


def test_a_second_get_of_the_same_key_never_reaches_the_transport() -> None:
    transport = _Scripted((200, b'{"n":1}', {}), (200, b'{"n":2}', {}))
    client = _client("test_cache", transport, cache_ttl=300.0)

    async def twice() -> tuple[object, object]:
        return await client.request("GET", "/same"), await client.request("GET", "/same")

    first, second = asyncio.run(twice())
    assert first == second == {"n": 1}
    assert len(transport.calls) == 1
    assert client.stats["cache_hits"] == 1


def test_a_recorded_fixture_replays_offline_and_a_missing_one_raises(tmp_path) -> None:
    record = tmp_path / "fixtures"
    recorder = _client("test_record", _Scripted((200, b'{"recorded":42}', {})), record_dir=record)
    assert asyncio.run(recorder.request("GET", "/fixture")) == {"recorded": 42}

    replay = PacedClient(
        surface="test_replay",
        base_url="https://scripted.test",
        **PACING,
        offline_dir=record,
    )
    assert asyncio.run(replay.request("GET", "/fixture")) == {"recorded": 42}
    # Offline mode never opens a client, so an absent fixture is an error
    # rather than a silent trip to the network.
    with pytest.raises(OfflineError):
        asyncio.run(replay.request("GET", "/never-recorded"))
