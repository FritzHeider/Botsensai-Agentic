"""Curation of the Telegram call-channel watchlist (P2-04).

`TelegramChannelCollector` has been able to read a public channel since P1-01.
What it never had was a list — `Pipeline._wire_social_handles` handed it a
literal `[]` — so the surface that can see a buy before the chart does was
reading nothing at all. These tests pin the two halves of fixing that.

**Discovery must not confuse a token's room with a call room.** Every pump.fun
launch publishes a Telegram link and almost all of them are that token's own
group. Measured on the real store 2026-08-04: of 120 launches with a Telegram
link, `t.me/CRYPTOLIQUIDBNB` is the link of seven distinct tokens and
`t.me/pisklauren` of three; every other link belongs to exactly one. Reuse is
the discriminator, and the tests below hold it at both ends — one token is never
enough, and a channel cannot manufacture reuse by linking to itself.

**Ranking must not measure noise.** The single most dangerous thing here is that
`market_snapshots` is not a price path: every collector in a sweep writes at the
same instant, and of 383 same-token clusters inside 30 seconds, 33 disagree by
more than 3x and the worst by a factor of 6.8 million. A ranking that took
`MAX(price)` after a call would hand its best score to whichever channel
happened to speak before a data-quality artefact. That is not a hypothetical —
it is the bug that once labelled a token a 149,878x over nine microseconds — so
`test_a_disagreeing_cluster_is_not_a_peak` is the load-bearing test in this file.

The rest is the standing rule of this codebase applied to a new surface: a
channel this system could not measure is reported as unmeasured. It is not
reported as bad, and it is never dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from botsensai.models import (
    Chain,
    CurveStage,
    Launch,
    Launchpad,
    MarketSnapshot,
    Platform,
    SocialPost,
    TokenRef,
)
from botsensai.store.db import Database
from botsensai.watchlist import (
    CALL_SOURCE,
    LINK_SOURCE,
    TEXT_SOURCE,
    Call,
    ChannelCandidate,
    ChannelRank,
    channel_calls,
    channels_in_text,
    collected_channels,
    discover_candidates,
    is_useless,
    measure_call,
    normalize_channel,
    rank_channels,
    seed_call_channels,
)

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

#: Two real-shaped Solana mints. Base58, 43-44 characters, no 0/O/I/l.
MINT_A = "A6aDH7pW6VtDpzY5CeuM7cZDoKkz8SE5NjUqoGfpump"
MINT_B = "CUYMG3SR4fscGkYrnre5A4bZpsmr9Y8kfJvuRKV4pump"
KEY_A = f"solana:{MINT_A}"
KEY_B = f"solana:{MINT_B}"


def _db(tmp_path, name: str = "watchlist.db") -> Database:
    return Database(str(tmp_path / name))


def _launch(mint: str, telegram: str | None = None, created_at: datetime = T0) -> Launch:
    return Launch(
        token=TokenRef(chain=Chain.SOLANA, mint=mint, symbol="TEST"),
        launchpad=Launchpad.PUMPFUN,
        deployer="DEV1",
        created_at=created_at,
        observed_at=created_at,
        telegram=telegram,
        source="dexscreener",
    )


def _snapshot(
    mint: str,
    offset_seconds: float,
    price_usd: float,
    source: str = "dexscreener",
) -> MarketSnapshot:
    stamp = T0 + timedelta(seconds=offset_seconds)
    return MarketSnapshot(
        token=TokenRef(chain=Chain.SOLANA, mint=mint, symbol="TEST"),
        as_of=stamp,
        observed_at=stamp,
        stage=CurveStage.BONDING,
        price_usd=price_usd,
        source=source,
    )


def _post(
    channel: str,
    offset_seconds: float,
    text: str = "",
    *,
    post_id: str | None = None,
    token_key: str | None = None,
    platform: Platform = Platform.TELEGRAM,
    observed_offset: float | None = None,
) -> SocialPost:
    stamp = T0 + timedelta(seconds=offset_seconds)
    seen = stamp if observed_offset is None else T0 + timedelta(seconds=observed_offset)
    return SocialPost(
        platform=platform,
        post_id=post_id or f"{channel}/{int(offset_seconds)}",
        token_key=token_key,
        author=channel,
        as_of=stamp,
        observed_at=seen,
        text=text,
        source="telegram",
    )


def _call(channel: str = "callroom", token_key: str = KEY_A, offset: float = 0.0) -> Call:
    return Call(
        channel=channel,
        token_key=token_key,
        post_id=f"{channel}/{int(offset)}",
        called_at=T0 + timedelta(seconds=offset),
    )


def _rank(channel: str, measured: int, peak: float | None, lead: float | None = 60.0) -> ChannelRank:
    return ChannelRank(
        channel=channel,
        calls=measured,
        measured=measured,
        median_lead_seconds=lead if measured else None,
        median_peak_multiple=peak,
        led_share=None if peak is None else float(peak > 1.0),
        entry_after_call=0,
        already_peaked=0,
        note="" if measured else "no calls collected yet",
    )


# --------------------------------------------------------------------------- #
# Channel identity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [
        "callroom",
        "@callroom",
        "CallRoom",
        "https://t.me/callroom",
        "http://t.me/CallRoom/",
        "https://www.t.me/callroom",
        "https://telegram.me/callroom",
        "https://t.me/s/callroom",
        "https://t.me/callroom?before=42",
        "join us at https://t.me/callroom for entries",
    ],
)
def test_every_way_a_channel_is_written_normalizes_to_one_handle(value):
    """`t.me` is case-insensitive and the store holds whatever a token published.

    Keying on the raw string would count `t.me/CallRoom` and `@callroom` as two
    channels, splitting one channel's record in half and halving the sample size
    behind every figure computed from it.
    """
    assert normalize_channel(value) == "callroom"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "https://t.me/+AbCdEfGhIjKl",
        "https://t.me/joinchat/AbCdEfGhIjKl",
        "https://t.me/c/1234567890/42",
        "https://t.me/s/",
        "https://t.me/share/url?url=x",
        "https://t.me/9lives",
        "abc",
        "https://twitter.com/callroom",
    ],
)
def test_what_is_not_a_public_channel_is_refused(value):
    """Private invites and Telegram's own deep links are not channels.

    `t.me/+…` and `joinchat` are invitation links to private groups, which the
    anonymous `t.me/s/` preview cannot render at all — putting one on the
    watchlist spends a fetch per sweep to be told nothing, forever.
    """
    assert normalize_channel(value) is None


def test_links_are_found_inside_prose_and_deduplicated():
    text = "mirror at t.me/alpharoom, backup https://t.me/s/alpharoom, dead t.me/+privateXYZ"
    assert channels_in_text(text) == ["alpharoom"]


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def test_one_token_is_a_community_room_and_two_are_a_call_channel(tmp_path):
    """The entire curation rule, at the boundary where it decides.

    Nearly every launch publishes a Telegram link and nearly every one of them is
    that token's own group. What distinguishes a call channel from the outside is
    that unrelated launches point at it.
    """
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A, telegram="https://t.me/ownroom"))
    db.upsert_launch(_launch(MINT_A, telegram="https://t.me/CallRoom"))
    db.upsert_launch(_launch(MINT_B, telegram="https://t.me/callroom"))

    found = {c.channel: c for c in discover_candidates(db, min_tokens=2)}
    assert "ownroom" not in found
    assert found["callroom"].tokens == 2
    assert LINK_SOURCE in found["callroom"].sources


def test_a_channel_cannot_manufacture_reuse_by_linking_to_itself(tmp_path):
    """Self-promotion is not third-party evidence.

    A room that pins its own invite gets that link credited once per message it
    ever posts. Left in, the cheapest way onto the watchlist would be to repeat
    your own handle, and the reuse score would stop measuring reuse.
    """
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.insert_posts(
        [
            _post("hypebot", 0, "join https://t.me/hypebot", post_id="p1", token_key=KEY_A),
            _post("hypebot", 60, "join https://t.me/HypeBot", post_id="p2", token_key=KEY_A),
        ]
    )
    found = {c.channel: c for c in discover_candidates(db, min_tokens=1)}
    assert TEXT_SOURCE not in found["hypebot"].sources
    assert found["hypebot"].tokens == 1


def test_a_third_party_post_on_another_platform_is_evidence(tmp_path):
    """How a call channel nobody links to in metadata gets found at all.

    Call channels advertise themselves in the X, 4chan and pump.fun text this
    system already collects, which is the only discovery path that works before
    the channel has ever been read.
    """
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.upsert_launch(_launch(MINT_B))
    db.insert_posts(
        [
            _post(
                "scout", 0, "called by t.me/alpharoom", post_id="p1",
                token_key=KEY_A, platform=Platform.X,
            ),
            _post(
                "scout", 60, "t.me/alpharoom again, also t.me/sidegroup", post_id="p2",
                token_key=KEY_B, platform=Platform.X,
            ),
        ]
    )
    found = {c.channel: c for c in discover_candidates(db, min_tokens=2)}
    assert set(found) == {"alpharoom"}, "sidegroup was named once, which is not reuse"
    assert found["alpharoom"].sources == (TEXT_SOURCE,)


def test_a_channel_no_launch_links_to_earns_its_place_by_calling(tmp_path):
    """The shape of a real call channel: it publishes mints, nobody publishes it.

    Counting reuse from links alone would never promote a room that calls a new
    mint every hour but that no token's metadata mentions — which is exactly the
    room worth reading.
    """
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.upsert_launch(_launch(MINT_B))
    db.insert_posts(
        [
            _post("alpharoom", 0, f"aping {MINT_A} now", post_id="c1"),
            _post("alpharoom", 60, f"next one {MINT_B}", post_id="c2"),
        ]
    )
    found = {c.channel: c for c in discover_candidates(db, min_tokens=2)}
    assert found["alpharoom"].tokens == 2
    assert found["alpharoom"].sources == (CALL_SOURCE,)
    assert found["alpharoom"].posts == 2


def test_re_reading_a_channel_does_not_multiply_its_messages(tmp_path):
    """Posts are stored once per observation, and a sweep re-reads every channel.

    `t.me/s/<name>` serves the last twenty messages every time it is fetched, so
    a channel read once an hour accumulates a row per message per sweep. Counted
    naively, an idle channel looks busier the longer it is watched, and every
    per-call median is computed over the same message repeated.
    """
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.insert_posts(
        [
            _post("alpharoom", 0, f"call {MINT_A}", post_id="c1", observed_offset=0),
            _post("alpharoom", 0, f"call {MINT_A}", post_id="c1", observed_offset=3600),
            _post("alpharoom", 0, f"call {MINT_A}", post_id="c1", observed_offset=7200),
        ]
    )
    assert collected_channels(db) == ["alpharoom"]
    assert len(channel_calls(db, ["alpharoom"])) == 1
    assert discover_candidates(db, min_tokens=1)[0].posts == 1


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #


def test_a_mint_the_store_never_saw_is_not_a_call(tmp_path):
    """A call about a token with no launch row has no path to be measured against.

    Accepting it would credit a channel with a call whose lead time is computed
    from nothing, and the store is full of base58-shaped strings that are wallets,
    signatures and pool addresses rather than mints.
    """
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.insert_posts(
        [
            _post("alpharoom", 0, f"buy {MINT_A}", post_id="c1"),
            _post("alpharoom", 60, f"buy {MINT_B}", post_id="c2"),
        ]
    )
    calls = channel_calls(db, ["alpharoom"])
    assert [c.token_key for c in calls] == [KEY_A]


def test_an_untagged_message_still_becomes_a_call(tmp_path):
    """Call channels are read token-agnostically, so their posts arrive untagged.

    `TelegramChannelCollector.enrich` tags only the messages it reads from a
    token's own room. A watchlist channel's messages carry no `token_key`, so a
    ranking that only looked at that column would find the watchlist has never
    made a call.
    """
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.insert_posts([_post("alpharoom", 0, f"entry {MINT_A} lfg", post_id="c1")])
    assert [c.token_key for c in channel_calls(db, ["alpharoom"])] == [KEY_A]


def test_a_channel_not_asked_about_is_not_ranked(tmp_path):
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.insert_posts([_post("otherroom", 0, f"buy {MINT_A}", post_id="c1")])
    assert channel_calls(db, ["alpharoom"]) == []


# --------------------------------------------------------------------------- #
# Lead time
# --------------------------------------------------------------------------- #


def test_lead_is_the_interval_to_the_peak_the_call_preceded(tmp_path):
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.insert_snapshots(
        [
            _snapshot(MINT_A, -60, 1.0),
            _snapshot(MINT_A, 600, 2.0),
            _snapshot(MINT_A, 1_200, 4.0),
            _snapshot(MINT_A, 1_800, 3.0),
        ]
    )
    result = measure_call(db, _call(offset=0.0), now=T0 + timedelta(hours=12))
    assert result is not None
    assert result.lead_seconds == 1_200.0
    assert result.peak_multiple == pytest.approx(4.0)
    assert result.entry_after_call is False
    assert result.already_peaked is False


def test_a_disagreeing_cluster_is_not_a_peak(tmp_path):
    """The one that decides whether this ranking means anything.

    Every collector in a sweep writes within a second or two of every other. Of
    383 same-token clusters inside 30 seconds on the real store, 33 disagree by
    more than 3x and the worst by a factor of 6.8 million. Read as a path, that
    disagreement is a price move — the bug that once produced a 149,878x label
    over nine microseconds. Here it would hand a channel a thousand-x record for
    having spoken before three collectors quoted the same moment differently.

    So the path is rebuilt with the labeller's own `collapse_path`, and the
    cluster below collapses to its median rather than to its outlier.
    """
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.insert_snapshots(
        [
            _snapshot(MINT_A, -60, 1.0, source="pumpfun"),
            _snapshot(MINT_A, 600, 1.0, source="dexscreener"),
            _snapshot(MINT_A, 601, 1_000.0, source="geckoterminal"),
            _snapshot(MINT_A, 602, 1.1, source="pumpfun"),
        ]
    )
    result = measure_call(db, _call(offset=0.0), now=T0 + timedelta(hours=12))
    assert result is not None
    assert result.peak_multiple == pytest.approx(1.1), (
        "the 1000x quote is three sources disagreeing at one instant, not a move"
    )


def test_a_call_with_no_path_after_it_is_unmeasured_not_zero(tmp_path):
    """Absence of evidence about a channel is not evidence against it.

    Returning a 0 lead or a 0.0 multiple here would drag the channel's median
    down and eventually drop it from the watchlist for the crime of being called
    while this system was not collecting.
    """
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.insert_snapshots([_snapshot(MINT_A, -600, 1.0), _snapshot(MINT_A, -60, 2.0)])
    assert measure_call(db, _call(offset=0.0), now=T0 + timedelta(hours=12)) is None


def test_a_peak_past_the_horizon_belongs_to_a_different_story(tmp_path):
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.insert_snapshots([_snapshot(MINT_A, -60, 1.0), _snapshot(MINT_A, 40_000, 50.0)])
    assert (
        measure_call(db, _call(offset=0.0), horizon_seconds=3_600.0, now=T0 + timedelta(days=2))
        is None
    )


def test_a_call_after_the_move_is_marked_as_calling_a_top(tmp_path):
    """A channel describing a move it did not lead must be visible as such."""
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.insert_snapshots(
        [
            _snapshot(MINT_A, -600, 1.0),
            _snapshot(MINT_A, -60, 10.0),
            _snapshot(MINT_A, 600, 6.0),
        ]
    )
    result = measure_call(db, _call(offset=0.0), now=T0 + timedelta(hours=12))
    assert result is not None
    assert result.already_peaked is True
    assert result.peak_multiple == pytest.approx(0.6)


def test_an_entry_taken_after_the_call_is_declared(tmp_path):
    """Measured on the real store 2026-08-04: this is the common case, not the edge.

    Every Telegram post in the store has zero snapshots at or before its `as_of`,
    because `t.me/s/` serves twenty messages that can predate collection by
    months. Pricing the entry at the first observation afterwards flatters the
    channel — any instant move is missed — so the approximation is counted and
    reported rather than folded silently into the median.
    """
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.insert_snapshots([_snapshot(MINT_A, 600, 1.0), _snapshot(MINT_A, 1_200, 3.0)])
    result = measure_call(db, _call(offset=0.0), now=T0 + timedelta(hours=12))
    assert result is not None
    assert result.entry_after_call is True
    assert result.peak_multiple == pytest.approx(3.0)
    assert result.lead_seconds == 1_200.0


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


def test_a_ranking_reports_the_sample_size_beside_the_median(tmp_path):
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.upsert_launch(_launch(MINT_B))
    db.insert_snapshots(
        [
            _snapshot(MINT_A, 0, 1.0),
            _snapshot(MINT_A, 600, 5.0),
            _snapshot(MINT_B, 0, 1.0),
            _snapshot(MINT_B, 1_800, 2.0),
        ]
    )
    db.insert_posts(
        [
            _post("alpharoom", -30, f"call {MINT_A}", post_id="c1"),
            _post("alpharoom", -30, f"call {MINT_B}", post_id="c2"),
        ]
    )
    ranking = rank_channels(db, ["alpharoom"], now=T0 + timedelta(hours=12))
    assert len(ranking) == 1
    entry = ranking[0]
    assert entry.calls == 2
    assert entry.measured == 2
    assert entry.median_lead_seconds == pytest.approx((630 + 1_830) / 2)
    assert entry.median_peak_multiple == pytest.approx(3.5)
    assert entry.led_share == 1.0


def test_a_channel_with_nothing_measured_comes_back_saying_so(tmp_path):
    """Dropping it from the output would read as a channel with a bad record."""
    db = _db(tmp_path)
    ranking = rank_channels(db, ["https://t.me/NeverRead"], now=T0)
    assert [r.channel for r in ranking] == ["neverread"]
    assert ranking[0].measured == 0
    assert ranking[0].median_lead_seconds is None
    assert ranking[0].note == "no calls collected yet"


def test_measured_channels_sort_ahead_of_unmeasured_ones(tmp_path):
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A))
    db.insert_snapshots([_snapshot(MINT_A, 0, 1.0), _snapshot(MINT_A, 600, 5.0)])
    db.insert_posts([_post("alpharoom", -30, f"call {MINT_A}", post_id="c1")])
    ranking = rank_channels(db, ["silentroom", "alpharoom"], now=T0 + timedelta(hours=12))
    assert [r.channel for r in ranking] == ["alpharoom", "silentroom"]


# --------------------------------------------------------------------------- #
# Dropping and seeding
# --------------------------------------------------------------------------- #


def test_a_channel_is_only_dropped_on_a_record_it_actually_has():
    """Both halves of the rule, each pinned by a case that satisfies the other."""
    assert is_useless(_rank("tops", measured=3, peak=1.0)) is True
    assert is_useless(_rank("tops", measured=2, peak=0.4)) is False, "two calls is not a record"
    assert is_useless(_rank("good", measured=9, peak=1.01)) is False
    assert is_useless(_rank("blind", measured=0, peak=None)) is False, (
        "never measured is not the same as measured bad"
    )
    assert is_useless(_rank("blind", measured=0, peak=None), min_calls=0) is False, (
        "the count is a caller's number; at zero there is still no median to judge"
    )


def test_the_curated_half_comes_first_and_the_discovered_half_follows():
    seeded = seed_call_channels(
        curated=["https://t.me/HandPicked", "@second"],
        candidates=[
            ChannelCandidate("discovered", tokens=9, posts=3, sources=(LINK_SOURCE,)),
            ChannelCandidate("handpicked", tokens=2, posts=0, sources=(LINK_SOURCE,)),
        ],
    )
    assert seeded == ["handpicked", "second", "discovered"]


def test_the_measurement_is_allowed_to_drop_a_hand_picked_channel():
    """The point of measuring is that the measurement wins.

    A channel a human added on outside evidence, which has since called three
    tops in this store, is not a channel that human would still choose.
    """
    seeded = seed_call_channels(
        curated=["tops", "keeper"],
        candidates=[],
        ranking=[_rank("tops", measured=4, peak=0.8), _rank("keeper", measured=4, peak=3.0)],
    )
    assert seeded == ["keeper"]


def test_an_unmeasured_channel_survives_the_ranking():
    seeded = seed_call_channels(
        curated=["blind"], candidates=[], ranking=[_rank("blind", measured=0, peak=None)]
    )
    assert seeded == ["blind"]


def test_the_watchlist_stops_at_the_collector_budget():
    """Each channel is a fetch per sweep against a 30/min pacing, shared with the
    room every token publishes for itself."""
    candidates = [
        ChannelCandidate(f"room{i}", tokens=5, posts=0, sources=(LINK_SOURCE,)) for i in range(20)
    ]
    assert len(seed_call_channels([], candidates, limit=6)) == 6


def test_the_same_channel_written_two_ways_is_seeded_once():
    seeded = seed_call_channels(
        curated=["https://t.me/AlphaRoom", "@alpharoom"],
        candidates=[ChannelCandidate("alpharoom", tokens=4, posts=1, sources=(LINK_SOURCE,))],
    )
    assert seeded == ["alpharoom"]


def test_a_private_invite_never_reaches_the_collector():
    assert seed_call_channels(curated=["https://t.me/+SecretInvite"], candidates=[]) == []


# --------------------------------------------------------------------------- #
# Store reads this module added
# --------------------------------------------------------------------------- #


def test_a_text_search_matches_literally_not_as_a_pattern(tmp_path):
    """`_` is a single-character wildcard in SQL LIKE, and handles contain it.

    `www_usdolly_rocks` is a real channel in the real store. Searched unescaped,
    it also matches `wwwXusdollyYrocks`, so a caller counting how often a
    channel is named gets a number inflated by every handle that differs at
    those positions. `.` is not a wildcard here — this is LIKE, not a regex —
    which is exactly why the mistake survives a casual test.
    """
    db = _db(tmp_path)
    db.insert_posts(
        [
            _post("scout", 0, "see t.me/www_usdolly_rocks", post_id="p1", platform=Platform.X),
            _post("scout", 60, "see t.me/wwwXusdollyYrocks", post_id="p2", platform=Platform.X),
            _post("scout", 120, "100% of the supply", post_id="p3", platform=Platform.X),
        ]
    )
    assert [p.post_id for p in db.posts_matching("www_usdolly_rocks")] == ["p1"]
    assert [p.post_id for p in db.posts_matching("100%")] == ["p3"]
    assert {p.post_id for p in db.posts_matching("t.me/")} == {"p1", "p2"}


def test_only_launch_link_columns_can_be_read(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValueError, match="not a launch link column"):
        db.social_links("token_key; DROP TABLE launches")


def test_the_collector_reads_only_as_many_channels_as_it_budgeted_for(tmp_path):
    """The seeding cap and the collector's cap are the same number for a reason.

    Each channel is one `t.me/s/<name>` fetch per sweep against a 30/min pacing
    that the sweep also owes to every token's own room. A watchlist longer than
    the budget does not read more channels — it reads the same ones and starves
    the token rooms behind them.
    """
    import asyncio

    from botsensai.collectors.social import TelegramChannelCollector
    from botsensai.config import Settings

    collector = TelegramChannelCollector(Settings())
    collector.config.extra["call_channels"] = [f"room{i}" for i in range(20)]

    class FakeClient:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def get_text(self, url: str, **kwargs) -> str:
            self.urls.append(url)
            return "<div>no messages today</div>"

    fake = FakeClient()
    collector._client = fake
    result = asyncio.run(collector.enrich([]))

    assert len(fake.urls) == TelegramChannelCollector.max_call_channels
    assert result.posts == []


# --------------------------------------------------------------------------- #
# Pipeline wiring
# --------------------------------------------------------------------------- #


def _pipeline(tmp_path, call_channels: list[str]):
    from botsensai.collectors.social import TelegramChannelCollector
    from botsensai.config import Settings
    from botsensai.pipeline import Pipeline

    settings = Settings()
    settings.db_path = str(tmp_path / "wire.db")
    settings.memory.path = str(tmp_path / "wire-memory.db")
    settings.collectors["telegram"].extra["call_channels"] = list(call_channels)
    pipeline = Pipeline(
        settings,
        db=_db(tmp_path, "wire.db"),
        collectors=[TelegramChannelCollector(settings)],
    )
    return pipeline, pipeline.collectors.get("telegram")


def test_a_sweep_hands_the_collector_a_list_instead_of_an_empty_one(tmp_path):
    """The whole of P2-04 in one assertion.

    `_wire_social_handles` set `call_channels` to a literal `[]` on every sweep,
    so the one surface that can see a buy before the chart does was reading
    nothing. Discovery costs no requests: the links are already in the store.
    """
    pipeline, telegram = _pipeline(tmp_path, [])
    pipeline.db.upsert_launch(_launch(MINT_A, telegram="https://t.me/CallRoom"))
    pipeline.db.upsert_launch(_launch(MINT_B, telegram="https://t.me/callroom"))

    pipeline._wire_social_handles([])
    assert telegram.config.extra["call_channels"] == ["callroom"]


def test_a_dropped_channel_does_not_come_back_next_sweep(tmp_path):
    """`call_channels` is both the config key and this method's output.

    Without copying the curated list aside on the first pass, the second sweep
    would read the first sweep's output back as if a human had chosen it. The
    watchlist could then only ever grow, and no measurement could remove
    anything from it — which is the half of this task that has the teeth.
    """
    pipeline, telegram = _pipeline(tmp_path, ["handpicked"])
    pipeline.db.upsert_launch(_launch(MINT_A, telegram="https://t.me/CallRoom"))
    pipeline.db.upsert_launch(_launch(MINT_B, telegram="https://t.me/callroom"))

    pipeline._wire_social_handles([])
    assert telegram.config.extra["call_channels"] == ["handpicked", "callroom"]

    # The candidate loses its second token; the next sweep must recompute rather
    # than re-read its own previous answer.
    pipeline.db.conn.execute("UPDATE launches SET telegram = NULL WHERE token_key = ?", (KEY_B,))
    pipeline.db.conn.commit()
    pipeline._wire_social_handles([])
    assert telegram.config.extra["call_channels"] == ["handpicked"]
    assert telegram.config.extra["curated_call_channels"] == ["handpicked"]


def test_a_broken_store_read_costs_the_watchlist_not_the_sweep(tmp_path):
    """A curation nicety must never be able to fail a collection sweep."""
    pipeline, telegram = _pipeline(tmp_path, ["https://t.me/HandPicked"])

    class Exploding:
        def __getattr__(self, name):
            raise RuntimeError("store is unavailable")

    pipeline.db = Exploding()
    pipeline._wire_social_handles([])
    assert telegram.config.extra["call_channels"] == ["handpicked"]


def test_the_ranking_covers_every_channel_read_not_only_the_watchlist(tmp_path):
    """Discovery answers who might be worth reading; ranking answers what happened.

    A room this system has read has a record whether or not it qualifies for the
    watchlist, and that record is what tells a curator to promote it.
    """
    db = _db(tmp_path)
    db.upsert_launch(_launch(MINT_A, telegram="https://t.me/ownroom"))
    db.insert_posts([_post("ownroom", 0, f"call {MINT_A}", post_id="c1")])
    assert collected_channels(db) == ["ownroom"]
    assert [c.channel for c in discover_candidates(db, min_tokens=2)] == []
