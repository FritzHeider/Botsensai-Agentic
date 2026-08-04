"""Curation of the Telegram call-channel watchlist.

`TelegramChannelCollector` has worked since P1-01. What it never had is a list:
`Pipeline._wire_social_handles` set `call_channels` to a literal `[]`, so the
one surface in this system that can see a buy *before* the chart does was
reading nothing. This module supplies the list, and — more importantly — the
evidence for dropping entries from it again.

Two things happen here.

**Discovery is free.** Dexscreener's `info.socials` entry of type `telegram` is
folded into `Launch.telegram` at parse time, so every Telegram link Dexscreener
has ever published for a token is already sitting in the store. The
discriminator between a token's own room and a call channel is *reuse*: measured
on the real store 2026-08-04, `t.me/CRYPTOLIQUIDBNB` is the published Telegram
link of seven distinct tokens and `t.me/pisklauren` of three, while every other
link in 120 launches belongs to exactly one. A room that several unrelated
tokens point at is not a token's room. The second source is `t.me/` links inside
the post text this system already collects, which is how a channel that no token
links to still gets found — call channels advertise themselves.

**Ranking is a measurement, not an opinion.** For each call the channel made, the
lead time is the interval from the message to the highest price the token
subsequently reached, and the peak multiple is that price over what the position
would have been opened at. A channel whose median peak multiple sits at or below
1.0 across enough calls is calling tops, and is dropped.

The one trap here is that `market_snapshots` is not a price path. Every collector
in a sweep writes at the same instant, and of 383 same-token clusters inside 30
seconds, 33 disagree by more than 3x and the worst by a factor of 6.8 million.
A naive `MAX(price_usd)` after a call would rank channels on data-quality noise
— the identical bug that once labelled a token a 149,878x over nine
microseconds. So the path is reconstructed with the labeller's own
`choose_denomination` → `points_from_snapshots` → `collapse_path`, not with a
second implementation that would have to learn the same lesson again.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from botsensai.labeller import (
    LabelPolicy,
    PricePoint,
    choose_denomination,
    collapse_path,
    points_from_snapshots,
)
from botsensai.models import Platform, SocialPost, utcnow
from botsensai.store.db import Database
from botsensai.util.logging import get_logger

log = get_logger(__name__)

#: How many channels the watchlist hands the collector. Matches
#: `TelegramChannelCollector.max_call_channels`, which is the real budget: each
#: channel is one `t.me/s/<name>` fetch per sweep against a 30/min pacing.
DEFAULT_WATCHLIST_LIMIT = 6

#: How far past a call to look for the peak. Six hours is roughly the window in
#: which a pump.fun launch either runs or is over; beyond it the "peak" is
#: increasingly a different token than the one that was called.
DEFAULT_HORIZON_SECONDS = 6 * 3600.0

#: Calls older than this are not evidence about a channel's behaviour today.
DEFAULT_CALL_WINDOW_SECONDS = 30 * 86_400.0

#: Measured calls a channel needs before it can be judged useless. Dropping a
#: channel on one or two calls would be dropping it on noise.
DEFAULT_MIN_CALLS_TO_JUDGE = 3

#: Median peak multiple at or below which a channel is calling tops.
DEFAULT_MIN_PEAK_MULTIPLE = 1.0

#: Where a candidate came from, reported so a human curating the list can see
#: whether the evidence is a token's own metadata or a third party's post.
LINK_SOURCE = "dexscreener-link"
TEXT_SOURCE = "post-text"
CALL_SOURCE = "own-calls"
CURATED_SOURCE = "curated"

_TME_LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?t(?:elegram)?\.me/(?:s/)?(\+?[A-Za-z0-9_]{1,64})",
    re.IGNORECASE,
)

#: Telegram public usernames: a leading letter, then letters, digits and
#: underscores. Anything else is a deep link, an invite or a typo — and the
#: leading-letter rule is what refuses a `t.me/+…` private invite, which is why
#: the link pattern above captures the `+` instead of skipping past it. A second
#: explicit check for the `+` would read like a guard and never fire.
_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

#: `t.me` paths that are features of Telegram rather than channels. `joinchat`
#: is a private invite, which cannot be read anonymously at all, and `c` is an
#: internal numeric channel reference.
_RESERVED_PATHS = frozenset(
    {
        "addstickers",
        "addtheme",
        "bg",
        "c",
        "confirmphone",
        "contact",
        "invoice",
        "iv",
        "joinchat",
        "login",
        "proxy",
        "s",
        "setlanguage",
        "share",
        "socks",
    }
)

#: Base58, 32-44 characters — a Solana mint as it appears in a call message.
_MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


# --------------------------------------------------------------------------- #
# Channel identity
# --------------------------------------------------------------------------- #


def normalize_channel(value: str | None) -> str | None:
    """One channel handle, lowercased, or None if this is not a public channel.

    Accepts a bare handle, an `@handle`, a `t.me/` or `telegram.me/` URL, and the
    `t.me/s/` preview form the collector itself fetches. Returns None for private
    invite links, Telegram's own deep-link paths and anything that is not a legal
    username, so callers never have to ask a second question about the result.

    Handles are lowercased because `t.me` is case-insensitive and the store holds
    whichever casing a token published; keying on the raw string would count one
    channel twice.
    """
    if not value:
        return None
    match = _TME_LINK_RE.search(value.strip())
    raw = match.group(1) if match else value.strip().lstrip("@")
    handle = raw.strip("/").split("/")[0].split("?")[0].strip()
    if not _HANDLE_RE.match(handle):
        return None
    lowered = handle.lower()
    return None if lowered in _RESERVED_PATHS else lowered


def channels_in_text(text: str | None) -> list[str]:
    """Every public channel a piece of text links to, in order of appearance."""
    found: list[str] = []
    for raw in _TME_LINK_RE.findall(text or ""):
        channel = normalize_channel(raw)
        if channel is not None and channel not in found:
            found.append(channel)
    return found


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ChannelCandidate:
    """A channel the store has evidence for, and what that evidence is."""

    channel: str
    tokens: int
    posts: int
    sources: tuple[str, ...]


def discover_candidates(
    db: Database,
    min_tokens: int = 2,
    since: datetime | None = None,
    limit: int = 50,
) -> list[ChannelCandidate]:
    """Channels that several distinct tokens point at, best evidence first.

    `min_tokens` is the whole curation rule. At 1 this returns every token's own
    community room and the list is worthless; at 2 it returns rooms that more
    than one unrelated launch has published, which is what a call channel looks
    like from the outside.
    """
    tokens: defaultdict[str, set[str]] = defaultdict(set)
    sources: defaultdict[str, set[str]] = defaultdict(set)

    for link, keys in db.social_links("telegram").items():
        channel = normalize_channel(link)
        if channel is not None:
            tokens[channel].update(keys)
            sources[channel].add(LINK_SOURCE)

    for post in db.posts_matching("t.me/", since=since):
        _credit_post_links(post, tokens, sources)

    counts = _post_counts(db, since)
    # A channel's own messages are the best evidence of what it is. Reuse
    # measured from links alone would never promote a room that no token links
    # to but that calls a new mint every hour — which is precisely the shape of
    # the channels worth reading.
    for call in channel_calls(db, sorted(counts), since=since):
        tokens[call.channel].add(call.token_key)
        sources[call.channel].add(CALL_SOURCE)

    candidates = [
        ChannelCandidate(
            channel=channel,
            tokens=len(keys),
            posts=counts.get(channel, 0),
            sources=tuple(sorted(sources[channel])),
        )
        for channel, keys in tokens.items()
        if len(keys) >= min_tokens
    ]
    candidates.sort(key=lambda c: (-c.tokens, -c.posts, c.channel))
    return candidates[:limit]


def _credit_post_links(
    post: SocialPost,
    tokens: defaultdict[str, set[str]],
    sources: defaultdict[str, set[str]],
) -> None:
    """Credit a post's `t.me/` links, ignoring a channel linking to itself.

    Self-promotion is not third-party evidence, and counting it would let a
    channel manufacture its own reuse score by posting its own link under every
    token it already reaches.
    """
    author = normalize_channel(post.author) if post.platform is Platform.TELEGRAM else None
    for channel in channels_in_text(f"{post.text or ''} {post.url or ''}"):
        if channel == author:
            continue
        sources[channel].add(TEXT_SOURCE)
        seen = tokens[channel]
        if post.token_key:
            seen.add(post.token_key)


def _post_counts(db: Database, since: datetime | None) -> dict[str, int]:
    """How many distinct messages the store already holds per channel."""
    counts: defaultdict[str, int] = defaultdict(int)
    for post in db.posts_by_platform(Platform.TELEGRAM, since=since):
        channel = normalize_channel(post.author)
        if channel is not None:
            counts[channel] += 1
    return dict(counts)


def collected_channels(db: Database, since: datetime | None = None) -> list[str]:
    """Every channel this system has actually read messages from.

    Distinct from `discover_candidates`, which answers "who might be worth
    reading". This answers "who have we already heard from", and it is the set
    worth ranking: a channel earns its way onto the watchlist by reuse, but the
    record of what its calls were worth exists for any room we have read,
    including the token's own. Ranking only ever removes from the watchlist, so
    widening what gets ranked cannot widen what gets collected.
    """
    return sorted(_post_counts(db, since))


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Call:
    """One message from a channel, about one token, at one moment."""

    channel: str
    token_key: str
    post_id: str
    called_at: datetime


def channel_calls(
    db: Database,
    channels: Iterable[str],
    since: datetime | None = None,
    max_per_channel: int = 200,
) -> list[Call]:
    """Every message from these channels that names a token we know about.

    A message reaches a token two ways: the collector already tagged it while
    reading the token's own room, or the message contains a mint that exists in
    `launches`. The second is the one that matters for a call channel, whose
    messages arrive untagged because the channel is read token-agnostically.
    """
    wanted = {c for c in (normalize_channel(x) for x in channels) if c is not None}
    if not wanted:
        return []

    resolve = _mint_resolver(db)
    seen: dict[tuple[str, str, str], Call] = {}
    per_channel: defaultdict[str, int] = defaultdict(int)
    for post in db.posts_by_platform(Platform.TELEGRAM, since=since):
        channel = normalize_channel(post.author)
        if channel is None or channel not in wanted or per_channel[channel] >= max_per_channel:
            continue
        per_channel[channel] += 1
        for token_key in _called_tokens(post, resolve):
            key = (channel, token_key, post.post_id)
            if key not in seen:
                seen[key] = Call(channel, token_key, post.post_id, post.as_of)
    return sorted(seen.values(), key=lambda c: c.called_at)


def _mint_resolver(db: Database) -> Callable[[str], str | None]:
    """Mint -> token key, but only for mints the store has a launch for.

    Accepting an unknown mint would attribute a call to a token this system
    never observed, and the lead time of a call against a price path that does
    not exist is not a small error — it is a channel scoring on nothing.
    """
    cache: dict[str, str | None] = {}

    def resolve(mint: str) -> str | None:
        if mint not in cache:
            key = f"solana:{mint}"
            cache[mint] = key if db.launch(key) is not None else None
        return cache[mint]

    return resolve


def _called_tokens(post: SocialPost, resolve: Callable[[str], str | None]) -> list[str]:
    keys: list[str] = []
    if post.token_key:
        keys.append(post.token_key)
    for mint in _MINT_RE.findall(post.text or ""):
        key = resolve(mint)
        if key is not None and key not in keys:
            keys.append(key)
    return keys


# --------------------------------------------------------------------------- #
# Lead time
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LeadMeasurement:
    """What one call was worth, and how honest the measurement of it is."""

    call: Call
    lead_seconds: float
    peak_multiple: float
    #: No price observation existed at the moment of the call, so the entry
    #: price is the first one afterwards. Reported rather than hidden: it
    #: flatters the channel, because the entry is taken after any instant move.
    entry_after_call: bool
    #: The path was already at least this high before the call. The channel is
    #: describing a move rather than leading one.
    already_peaked: bool


def measure_call(
    db: Database,
    call: Call,
    policy: LabelPolicy | None = None,
    horizon_seconds: float = DEFAULT_HORIZON_SECONDS,
    now: datetime | None = None,
) -> LeadMeasurement | None:
    """Lead time and peak multiple for one call, or None if unmeasurable.

    None means exactly that: this system holds no price path after the call.
    It is not a zero, and `rank_channels` counts it as unmeasured rather than
    letting it drag a median down.
    """
    policy = policy or LabelPolicy()
    path = _price_path(db, call.token_key, policy, now)
    if not path:
        return None

    horizon_end = call.called_at + timedelta(seconds=horizon_seconds)
    after = [p for p in path if call.called_at < p.as_of <= horizon_end]
    if not after:
        return None
    before = [p for p in path if p.as_of <= call.called_at]

    entry = before[-1] if before else after[0]
    if entry.price <= 0:
        return None
    peak = max(after, key=lambda p: p.high)
    prior_high = max((p.high for p in before), default=0.0)
    return LeadMeasurement(
        call=call,
        lead_seconds=(peak.as_of - call.called_at).total_seconds(),
        peak_multiple=peak.high / entry.price,
        entry_after_call=not before,
        already_peaked=bool(before) and prior_high >= peak.high,
    )


def _price_path(
    db: Database, token_key: str, policy: LabelPolicy, now: datetime | None
) -> list[PricePoint]:
    """The token's path, reconstructed the way the labeller reconstructs it."""
    snapshots = db.snapshots_as_of(token_key, now or utcnow())
    if not snapshots:
        return []
    points = points_from_snapshots(snapshots, choose_denomination(snapshots))
    return [p for p in collapse_path(points, policy.path_bucket_seconds) if p.price > 0]


@dataclass(frozen=True)
class ChannelRank:
    """A channel's record, with the sample size beside every figure."""

    channel: str
    calls: int
    measured: int
    median_lead_seconds: float | None
    median_peak_multiple: float | None
    #: Share of measured calls where the price went above the entry afterwards.
    led_share: float | None
    entry_after_call: int
    already_peaked: int
    note: str


def rank_channels(
    db: Database,
    channels: Iterable[str],
    policy: LabelPolicy | None = None,
    horizon_seconds: float = DEFAULT_HORIZON_SECONDS,
    since: datetime | None = None,
    now: datetime | None = None,
) -> list[ChannelRank]:
    """Rank channels by what their calls were worth, best first.

    Every channel asked about comes back, including ones with no calls and ones
    whose calls have no price path. A channel missing from the output would read
    as a channel with nothing to show for itself, and those are two different
    facts.
    """
    policy = policy or LabelPolicy()
    wanted = sorted({c for c in (normalize_channel(x) for x in channels) if c is not None})
    calls_by_channel: defaultdict[str, list[Call]] = defaultdict(list)
    for call in channel_calls(db, wanted, since=since):
        calls_by_channel[call.channel].append(call)

    ranks: list[ChannelRank] = []
    for channel in wanted:
        calls = calls_by_channel.get(channel, [])
        measurements = [
            m
            for m in (
                measure_call(db, call, policy, horizon_seconds, now) for call in calls
            )
            if m is not None
        ]
        ranks.append(_summarize(channel, calls, measurements))
    ranks.sort(key=_rank_order)
    return ranks


def _summarize(
    channel: str, calls: Sequence[Call], measurements: Sequence[LeadMeasurement]
) -> ChannelRank:
    if not measurements:
        note = "no calls collected yet" if not calls else "no price path after any call"
        return ChannelRank(channel, len(calls), 0, None, None, None, 0, 0, note)
    led = sum(1 for m in measurements if m.peak_multiple > 1.0)
    return ChannelRank(
        channel=channel,
        calls=len(calls),
        measured=len(measurements),
        median_lead_seconds=median(m.lead_seconds for m in measurements),
        median_peak_multiple=median(m.peak_multiple for m in measurements),
        led_share=led / len(measurements),
        entry_after_call=sum(1 for m in measurements if m.entry_after_call),
        already_peaked=sum(1 for m in measurements if m.already_peaked),
        note="",
    )


def _rank_order(rank: ChannelRank) -> tuple[int, float, int, str]:
    """Measured channels first, then by what a call was worth."""
    return (
        0 if rank.measured else 1,
        -(rank.median_peak_multiple or 0.0),
        -rank.measured,
        rank.channel,
    )


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #


def is_useless(
    rank: ChannelRank,
    min_calls: int = DEFAULT_MIN_CALLS_TO_JUDGE,
    min_peak_multiple: float = DEFAULT_MIN_PEAK_MULTIPLE,
) -> bool:
    """Whether the evidence positively says this channel is not worth reading.

    Both halves matter. A channel is only dropped once it has made enough
    measured calls to have a record, and only if that record says the price did
    not go up after it spoke. A channel this system has never managed to measure
    is never dropped — that would be treating our own blindness as its failure,
    which is the same mistake as scoring a dead surface bearish.

    The `is not None` clause is not implied by the count. `min_calls` is a
    caller's number, and at zero every never-measured channel reaches the
    comparison with no median to compare.
    """
    return (
        rank.measured >= min_calls
        and rank.median_peak_multiple is not None
        and rank.median_peak_multiple <= min_peak_multiple
    )


def seed_call_channels(
    curated: Iterable[str],
    candidates: Iterable[ChannelCandidate] = (),
    ranking: Iterable[ChannelRank] = (),
    limit: int = DEFAULT_WATCHLIST_LIMIT,
    min_calls: int = DEFAULT_MIN_CALLS_TO_JUDGE,
    min_peak_multiple: float = DEFAULT_MIN_PEAK_MULTIPLE,
) -> list[str]:
    """The watchlist the collector should read this sweep.

    Curated entries come first because a human put them there on evidence this
    store does not hold, then discovered candidates in evidence order. The
    ranking only ever subtracts — it can neither add a channel nor reorder one —
    and it subtracts from the curated half too. A hand-added channel that has
    measurably called three tops is not a channel a human would still want; the
    whole point of measuring is that the measurement is allowed to win.
    """
    dropped = {r.channel for r in ranking if is_useless(r, min_calls, min_peak_multiple)}
    seeded: list[str] = []
    for value in [*curated, *(c.channel for c in candidates)]:
        channel = normalize_channel(value)
        if channel is None or channel in dropped or channel in seeded:
            continue
        seeded.append(channel)
        if len(seeded) >= limit:
            break
    return seeded


def build_watchlist(
    db: Database,
    curated: Iterable[str] = (),
    limit: int = DEFAULT_WATCHLIST_LIMIT,
    min_tokens: int = 2,
    horizon_seconds: float = DEFAULT_HORIZON_SECONDS,
    window_seconds: float = DEFAULT_CALL_WINDOW_SECONDS,
    now: datetime | None = None,
) -> list[str]:
    """Discover, rank and seed in one call — what the pipeline needs each sweep."""
    curated = list(curated)
    since = (now or utcnow()) - timedelta(seconds=window_seconds)
    candidates = discover_candidates(db, min_tokens=min_tokens, since=since)
    ranking = rank_channels(
        db,
        [*curated, *(c.channel for c in candidates)],
        horizon_seconds=horizon_seconds,
        since=since,
        now=now,
    )
    return seed_call_channels(curated, candidates, ranking, limit=limit)


__all__ = [
    "CALL_SOURCE",
    "CURATED_SOURCE",
    "DEFAULT_CALL_WINDOW_SECONDS",
    "DEFAULT_HORIZON_SECONDS",
    "DEFAULT_MIN_CALLS_TO_JUDGE",
    "DEFAULT_MIN_PEAK_MULTIPLE",
    "DEFAULT_WATCHLIST_LIMIT",
    "LINK_SOURCE",
    "TEXT_SOURCE",
    "Call",
    "ChannelCandidate",
    "ChannelRank",
    "LeadMeasurement",
    "build_watchlist",
    "channel_calls",
    "channels_in_text",
    "collected_channels",
    "discover_candidates",
    "is_useless",
    "measure_call",
    "normalize_channel",
    "rank_channels",
    "seed_call_channels",
]
