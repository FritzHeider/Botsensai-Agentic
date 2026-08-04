"""Social collectors — the web-use half of the system.

X, Instagram and TikTok do not publish the data these metrics need, and the
official APIs that expose fragments of it are priced for enterprises rather than
for a research bot watching thousands of tokens. So the approach is the one the
brief specified: read the pages a person can read, and where a page fetches its
own JSON, read that JSON off the wire.

Every endpoint here was probed live on 2026-07-25 and several widely-cited ones
turned out to be dead — **in the most dangerous possible way**. Three
`cdn.syndication.twimg.com` paths that most scraper libraries still call return
HTTP 200 with a zero-byte body and no content type. Not a 404, not a 429: a
success status with nothing in it. A collector that trusts the status code
records "no social activity" for every token forever and never raises an alarm.
That is precisely the silent-absence failure this codebase exists to avoid, so
`_require_body` treats an empty success as an error rather than as data.

Design rules:

* **Public data only, at human pace.** Every collector reads pages any visitor
  can open. Nothing logs in on someone else's behalf, solves a challenge, or
  submits anything.
* **Measured limits, not guessed ones.** `syndication.twitter.com` allows roughly
  twelve requests per fifteen minutes per IP before hard-429ing, and stays 429 for
  around ten minutes. `cdn.syndication.twimg.com/tweet-result` showed no
  throttling at all across forty consecutive calls. Those two facts have opposite
  design consequences and both are encoded below.
* **Every surface degrades independently.** One platform going dark must not stop
  the others, and must never be reported as a bearish reading.
"""

from __future__ import annotations

import contextlib
import html as html_lib
import json
import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from botsensai.collectors.base import CollectionResult, Collector, tag_posts
from botsensai.collectors.browser import BrowserUnavailableError, WebUseDriver, get_driver
from botsensai.config import Settings
from botsensai.media.phash import exact_label
from botsensai.models import (
    Platform,
    SocialAccount,
    SocialPost,
    TokenRef,
    utcnow,
)
from botsensai.util.http import PacedClient
from botsensai.util.logging import get_logger
from botsensai.util.text import contains_address, extract_cashtags

log = get_logger(__name__)

# --- X -------------------------------------------------------------------- #
#: Live profile timeline. Returns HTML with the payload in a __NEXT_DATA__ tag.
X_TIMELINE_HOST = "https://syndication.twitter.com"
#: Live single-tweet endpoint. Different host, different (much looser) bucket.
X_TWEET_HOST = "https://cdn.syndication.twimg.com"

#: Measured: 12 requests then HTTP 429, still 429 at t+311s, recovered near
#: t+600s. That is ~12 per 15 minutes, so under one request per minute.
X_TIMELINE_RPM = 0.7
#: Measured: 40/40 consecutive requests returned 200 with no throttling.
X_TWEET_RPM = 40.0

#: GraphQL operations worth capturing off the search page. Matched as URL
#: substrings on captured responses, not as a path, because X renames the query
#: id in the path on every deploy and never the operation name.
X_SEARCH_PATTERN = r"(SearchTimeline|TweetDetail|TweetResultByRestId)"

#: Posts one search harvest aims for before it stops scrolling. Set from what
#: the metrics need rather than a round number: `engager_age_dispersion` needs 8
#: accounts with known creation dates and `reply_template_ratio` needs 12
#: replies, and on a live ticker feed only a minority of captured posts carry
#: either. 200 clears both with room for the ones that carry neither.
X_SEARCH_POST_TARGET = 200
#: Ceiling on one authenticated search harvest. Reached only by a feed that
#: keeps paying out; the idle-scroll stop ends a quiet ticker in seconds.
X_SEARCH_DEADLINE_SECONDS = 180.0
#: The anonymous path gets a much smaller ceiling on purpose. x.com/search
#: serves a login wall to a logged-out visitor, so the scrolls after the first
#: capture nothing, and spending three minutes discovering that once per token
#: would cost the sweep more than the whole surface is worth without a session.
X_PUBLIC_SEARCH_DEADLINE_SECONDS = 25.0
X_SEARCH_MAX_SCROLLS = 40
#: Consecutive scrolls that may return no new GraphQL before the harvest ends.
X_SEARCH_IDLE_SCROLLS = 2

#: These return HTTP 200 with a zero-byte body. Kept as a named constant so the
#: next person to "helpfully" reintroduce one finds this note first.
X_DEAD_ENDPOINTS = (
    "cdn.syndication.twimg.com/timeline/profile",
    "cdn.syndication.twimg.com/timeline/tweet",
    "cdn.syndication.twimg.com/widgets/timelines",
)

# --- Reddit --------------------------------------------------------------- #
#: Reddit's own .json endpoints return HTTP 403 with an HTML interstitial to
#: datacenter IPs. Arctic Shift mirrors the full post schema and does not.
ARCTIC_SHIFT = "https://arctic-shift.photon-reddit.com/api"
REDDIT_BASE = "https://www.reddit.com"

# --- 4chan ---------------------------------------------------------------- #
FOURCHAN_API = "https://a.4cdn.org"
FOURCHAN_CDN = "https://i.4cdn.org"
#: Documented hard rule: no more than one request per second.
FOURCHAN_RPM = 55

# --- Telegram ------------------------------------------------------------- #
TELEGRAM_PREVIEW = "https://t.me/s"

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE
)
_TG_MESSAGE_RE = re.compile(
    r'<div class="tgme_widget_message[^"]*"[^>]*data-post="([^"]+)"(.*?)(?=<div class="tgme_widget_message |\Z)',
    re.DOTALL,
)
_TG_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.DOTALL
)
_TG_TIME_RE = re.compile(r'<time[^>]+datetime="([^"]+)"')
_TG_VIEWS_RE = re.compile(r'<span class="tgme_widget_message_views">([^<]+)</span>')
_TAG_RE = re.compile(r"<[^>]+>")


class EmptySuccessError(RuntimeError):
    """A 2xx response with no usable body.

    Raised rather than returned because an empty success from a social endpoint
    is indistinguishable, downstream, from "this token has no social activity" —
    and those two things must never be confused.
    """


def _require_body(payload: Any, url: str) -> Any:
    if payload is None or payload == "" or payload == [] or payload == {}:
        raise EmptySuccessError(
            f"{url} returned success with an empty body; treating as failure, not as absence"
        )
    return payload


def _iso(value: Any) -> datetime | None:
    """Parse the several timestamp formats these surfaces mix together."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 1e12:
            raw /= 1000.0
        with contextlib.suppress(OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(raw, tz=UTC)
        return None
    text = str(value).replace("Z", "+00:00")
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    with contextlib.suppress(ValueError, TypeError):
        # X's v1.1 format: "Fri Jul 24 22:40:18 +0000 2026"
        return datetime.strptime(str(value), "%a %b %d %H:%M:%S %z %Y")
    return None


def _author_rank(post: SocialPost) -> int:
    """How much of an author one parse of a post recovered.

    The structural walker yields both a tweet node and that node's own `legacy`
    sub-dict, and only the outer one carries the author. Both parse to the same
    post id, so a dedupe that simply keeps whichever arrived first will
    sometimes file a post under `unknown` with no creation date — turning a real
    author distribution into a fabricated one and blinding
    `engager_age_dispersion` for that post. Rank decides ties instead.
    """
    resolved = not (post.author == "unknown" or post.author.startswith("id:"))
    return int(post.author_created_at is not None) + int(resolved)


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        with contextlib.suppress(TypeError, ValueError):
            return int(float(value))
    return None


def _abbrev_int(text: str | None) -> int | None:
    """Telegram renders view counts as '3.46M' / '1.44K' / '251'."""
    if not text:
        return None
    cleaned = text.strip().upper().replace(",", "")
    multiplier = 1
    if cleaned.endswith("K"):
        multiplier, cleaned = 1_000, cleaned[:-1]
    elif cleaned.endswith("M"):
        multiplier, cleaned = 1_000_000, cleaned[:-1]
    elif cleaned.endswith("B"):
        multiplier, cleaned = 1_000_000_000, cleaned[:-1]
    with contextlib.suppress(ValueError):
        return int(float(cleaned) * multiplier)
    return None


def _strip_html(text: str) -> str:
    """Tags out, entities decoded.

    Entity decoding matters more than it looks: both 4chan and Telegram encode
    `$` as `&#036;`, so a naive strip leaves every cashtag unreadable and the
    ticker-extraction regex matches nothing at all.
    """
    stripped = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", html_lib.unescape(stripped)).strip()


def x_tweet_token(tweet_id: str) -> str:
    """Reproduce the obfuscation token `tweet-result` requires.

    The endpoint is unauthenticated but rejects requests without a `token`
    derived from the tweet id. The client-side derivation is
    `((id / 1e15) * Math.PI).toString(36).replace(/(0+|\\.)/g, '')`, reimplemented
    here because it is the difference between this path working and not.
    """
    try:
        numeric = float(tweet_id)
    except (TypeError, ValueError):
        return "a"
    value = (numeric / 1e15) * math.pi

    # JavaScript's Number.toString(36) on a float: integer part in base 36, then
    # a fractional expansion. Reproduced to enough precision for the check.
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    integer_part = int(value)
    fraction = value - integer_part

    out = ""
    if integer_part == 0:
        out = "0"
    else:
        n = integer_part
        while n > 0:
            n, rem = divmod(n, 36)
            out = digits[rem] + out

    frac_digits = ""
    for _ in range(20):
        fraction *= 36
        digit = int(fraction)
        frac_digits += digits[digit]
        fraction -= digit
        if fraction <= 0:
            break

    combined = f"{out}.{frac_digits}" if frac_digits else out
    return re.sub(r"(0+|\.)", "", combined) or "a"


class XCollector(Collector):
    """X via the two live syndication paths, with a browser fallback.

    The two hosts have completely independent rate limits and that asymmetry
    drives the design. The profile timeline is the only free source of follower
    counts and account ages, and it allows about twelve requests per quarter
    hour — so it is spent only on a token's own named account, cached for an
    hour, and never used for search. The per-tweet endpoint is effectively
    unthrottled but returns only `favorite_count` and `conversation_count`, so
    it is used to poll engagement *velocity* on posts already discovered.

    What this yields that no market API does: per-engager account creation dates
    (bot fleets are provisioned in batches), reply timestamps for cadence
    analysis, and `fast_followers_count` — X's own count of low-quality
    followers, which is as close to a purchased-follower oracle as exists.
    """

    name = "x"
    description = "X timelines, engagement velocity and engager metadata via live syndication"
    can_discover = False
    can_enrich = True
    #: Seconds one search harvest may spend. The session subclass raises this;
    #: see the constant for why the anonymous path is not given the same budget.
    search_deadline_seconds = X_PUBLIC_SEARCH_DEADLINE_SECONDS

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self._tweets: PacedClient | None = None
        self._browser_failed = False

    def default_headers(self) -> dict[str, str]:
        return {
            "Referer": "https://platform.twitter.com/",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        }

    @property
    def tweets(self) -> PacedClient:
        """Separate pacer for the unthrottled per-tweet host.

        Sharing one bucket with the profile host would throw away a 40/min
        allowance to protect a 0.7/min one.
        """
        if self._tweets is None:
            self._tweets = PacedClient(
                f"{self.name}:tweet-result",
                requests_per_minute=X_TWEET_RPM,
                max_concurrency=3,
                timeout=self.config.timeout_seconds,
                max_retries=2,
                cache_ttl=20.0,
                headers=self.default_headers(),
            )
        return self._tweets

    async def aclose(self) -> None:
        await super().aclose()
        if self._tweets is not None:
            await self._tweets.aclose()
            self._tweets = None

    async def health_check(self) -> bool:
        return self.config.enabled

    # -- profile timeline --------------------------------------------------- #

    async def profile_timeline(
        self, handle: str, limit: int = 40
    ) -> tuple[list[SocialPost], SocialAccount | None]:
        """Public timeline plus the account object, from the live HTML path.

        Cached for an hour by default: at twelve requests per fifteen minutes,
        re-fetching the same handle inside a sweep would consume the entire
        budget for one token.
        """
        handle = handle.strip().lstrip("@").split("/")[-1]
        if not handle:
            return [], None

        url = f"{X_TIMELINE_HOST}/srv/timeline-profile/screen-name/{handle}"
        try:
            html = await self.client.get_text(url, cache_ttl=3600.0)
            _require_body(html, url)
        except Exception as exc:
            log.debug("x.timeline_failed", handle=handle, error=str(exc))
            return [], None

        match = _NEXT_DATA_RE.search(html)
        if not match:
            log.debug("x.no_next_data", handle=handle)
            return [], None
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            return [], None

        timeline = (
            payload.get("props", {}).get("pageProps", {}).get("timeline", {}) or {}
        )
        entries = timeline.get("entries") or []
        if not entries:
            # Verified real behaviour: some accounts return an empty timeline with
            # HTTP 200. That is genuine absence for this handle, not an error.
            log.debug("x.empty_timeline", handle=handle)
            return [], None

        posts: list[SocialPost] = []
        account: SocialAccount | None = None
        for entry in entries[:limit]:
            tweet = (entry.get("content") or {}).get("tweet") or {}
            if not tweet:
                continue
            post = self._parse_tweet(tweet)
            if post is not None:
                posts.append(post)
            if account is None:
                account = self._parse_account(tweet.get("user") or {})
        return posts, account

    def _parse_account(self, user: dict[str, Any]) -> SocialAccount | None:
        if not user:
            return None
        handle = user.get("screen_name")
        if not handle:
            return None
        # Verified: `user.id` is ALWAYS 0 in this payload. Keying on it collapses
        # every account into one bucket, which would silently destroy every
        # per-account metric downstream.
        account_id = str(user.get("id_str") or "") or None
        return SocialAccount(
            platform=Platform.X,
            handle=str(handle),
            account_id=account_id,
            created_at=_iso(user.get("created_at")),
            followers=_int(user.get("followers_count")),
            following=_int(user.get("friends_count")),
            post_count=_int(user.get("statuses_count")),
            verified=bool(user.get("verified") or user.get("is_blue_verified")),
            verified_type=user.get("verified_type"),
            bio=user.get("description"),
            fast_followers=_int(user.get("fast_followers_count")),
            normal_followers=_int(user.get("normal_followers_count")),
        )

    def _parse_tweet(self, tweet: dict[str, Any]) -> SocialPost | None:
        post_id = str(tweet.get("id_str") or "").strip()
        created = _iso(tweet.get("created_at"))
        if not post_id or created is None:
            return None

        user = tweet.get("user") or {}
        handle = user.get("screen_name") or "unknown"
        text = str(tweet.get("full_text") or tweet.get("text") or "")

        media_urls: list[str] = []
        for block in ("extended_entities", "entities"):
            entities = tweet.get(block) or {}
            for item in entities.get("media") or []:
                if isinstance(item, dict) and item.get("media_url_https"):
                    media_urls.append(str(item["media_url_https"]))

        return SocialPost(
            platform=Platform.X,
            post_id=post_id,
            author=str(handle),
            author_id=str(user.get("id_str") or "") or None,
            as_of=created,
            observed_at=utcnow(),
            text=text,
            lang=tweet.get("lang"),
            url=f"https://x.com/{handle}/status/{post_id}",
            parent_id=str(tweet.get("in_reply_to_status_id_str") or "") or None,
            is_repost=bool(tweet.get("retweeted_status")),
            quoted_id=str(tweet.get("quoted_status_id_str") or "") or None,
            likes=_int(tweet.get("favorite_count")),
            replies=_int(tweet.get("reply_count") or tweet.get("conversation_count")),
            reposts=_int(tweet.get("retweet_count")),
            quotes=_int(tweet.get("quote_count")),
            author_followers=_int(user.get("followers_count")),
            author_created_at=_iso(user.get("created_at")),
            mentioned_tokens=extract_cashtags(text),
            media_urls=media_urls,
            source=f"{self.name}:syndication-timeline",
        )

    # -- per-tweet velocity ------------------------------------------------- #

    async def tweet_engagement(self, tweet_id: str) -> dict[str, Any] | None:
        """Current engagement on one post, from the unthrottled host.

        Only `favorite_count` and `conversation_count` are returned — verified by
        a full key dump, this endpoint carries no retweet, quote or reply counts.
        Its value is that it can be polled every few seconds without penalty,
        which turns it into an engagement-*velocity* probe. The inter-arrival
        regularity of that series is what `reply_rhythm_naturalness` consumes.
        """
        url = f"{X_TWEET_HOST}/tweet-result"
        try:
            payload = await self.tweets.get_json(
                url,
                params={"id": tweet_id, "token": x_tweet_token(tweet_id), "lang": "en"},
                cache_ttl=15.0,
            )
            _require_body(payload, url)
        except Exception as exc:
            log.debug("x.tweet_result_failed", tweet_id=tweet_id, error=str(exc))
            return None
        if not isinstance(payload, dict):
            return None
        return {
            "id": payload.get("id_str"),
            "favorite_count": _int(payload.get("favorite_count")),
            "conversation_count": _int(payload.get("conversation_count")),
            "created_at": _iso(payload.get("created_at")),
            "text": payload.get("text"),
            "observed_at": utcnow(),
        }

    # -- browser fallback --------------------------------------------------- #

    def search_budget(self) -> tuple[int, float]:
        """How many posts one search aims for, and how long it may spend.

        Both are overridable per surface through `collectors.x.extra` so an
        operator can trade sweep latency for depth without editing code.
        """
        extra = self.config.extra
        target = int(extra.get("search_post_target", X_SEARCH_POST_TARGET) or 0)
        deadline = float(
            extra.get("search_deadline_seconds", self.search_deadline_seconds) or 0.0
        )
        return max(1, target), max(0.0, deadline)

    async def harvest_posts(
        self,
        driver: WebUseDriver,
        url: str,
        pattern: str,
        *,
        limit: int | None = None,
        deadline_seconds: float | None = None,
        requests_per_minute: float | None = None,
        wait_ms: int = 5000,
    ) -> list[SocialPost]:
        """Scroll an X feed, absorbing its own GraphQL until the budget is spent.

        One page of search GraphQL is roughly twenty posts, which is below the
        evidence floor of every metric this feed exists to serve — so a single
        capture reads as "not enough data" on a token that is in fact being
        discussed heavily. Scrolling is what turns this surface from decorative
        into usable.

        Dedupe is by post id and is not optional: X re-serves the top of the
        feed on nearly every scroll, so a naive concatenation counts the same
        post four or five times. That inflates every count-based metric and,
        worse, concentrates the apparent author distribution onto whoever posted
        the item that keeps being re-served — which reads downstream as one
        account dominating the conversation, a shill fingerprint we would have
        manufactured ourselves.

        Insertion order is preserved, so the cap keeps the posts the feed ranked
        first rather than an arbitrary subset.
        """
        target, budget = self.search_budget()
        cap = target if limit is None else max(1, limit)
        posts: dict[str, SocialPost] = {}

        def absorb(bodies: list[Any]) -> bool:
            for body in bodies:
                for node in self._walk_for_tweets(body):
                    post = self._parse_graphql_tweet(node)
                    if post is None:
                        continue
                    held = posts.get(post.post_id)
                    if held is None or _author_rank(post) > _author_rank(held):
                        posts[post.post_id] = post
            return len(posts) < cap

        await driver.harvest_json(
            url,
            pattern,
            on_batch=absorb,
            surface=self.name,
            wait_ms=wait_ms,
            scroll_pause_ms=1400,
            max_scrolls=X_SEARCH_MAX_SCROLLS,
            deadline_seconds=budget if deadline_seconds is None else deadline_seconds,
            idle_scrolls=X_SEARCH_IDLE_SCROLLS,
            requests_per_minute=(
                float(self.config.requests_per_minute)
                if requests_per_minute is None
                else requests_per_minute
            ),
        )
        return list(posts.values())[:cap]

    async def harvest_search(
        self,
        driver: WebUseDriver,
        query: str,
        *,
        pattern: str = X_SEARCH_PATTERN,
        limit: int | None = None,
        deadline_seconds: float | None = None,
        requests_per_minute: float | None = None,
    ) -> list[SocialPost]:
        """Deep search for one query.

        The query is percent-encoded rather than interpolated raw. A ticker is
        operator-supplied text: an unencoded `#` truncates the URL at the
        fragment and searches for something else entirely, and an unencoded `&`
        appends a parameter — both of which return a plausible page of results
        for the wrong query, which is worse than an error.
        """
        return await self.harvest_posts(
            driver,
            f"https://x.com/search?q={quote(query, safe='')}&f=live",
            pattern,
            limit=limit,
            deadline_seconds=deadline_seconds,
            requests_per_minute=requests_per_minute,
        )

    async def search_via_browser(self, query: str, limit: int | None = None) -> list[SocialPost]:
        """Load the X search page and capture the GraphQL it fetches for itself.

        Search is not available on any free unauthenticated path, and reply text
        — which the template-clustering metric needs — is not in the syndication
        payloads either. Both come from here or not at all.
        """
        if self._browser_failed:
            return []
        try:
            # Inside the try: obtaining the driver is itself a step that can
            # fail, and `enrich` must never raise out of this collector.
            driver = get_driver(self.settings.browser)
            return await self.harvest_search(driver, query, limit=limit)
        except BrowserUnavailableError as exc:
            self._browser_failed = True
            log.info("x.browser_unavailable", error=str(exc))
            return []
        except Exception as exc:
            log.debug("x.browser_search_failed", query=query, error=str(exc))
            return []

    @staticmethod
    def _walk_for_tweets(payload: Any) -> list[dict[str, Any]]:
        """Find tweet-shaped objects structurally rather than by a fixed path.

        The GraphQL envelope has been reshaped repeatedly. Matching on the
        presence of the fields a tweet has survives renames that a hardcoded
        path does not.
        """
        found: list[dict[str, Any]] = []

        # The cap has to clear the deepest envelope we actually receive, not a
        # round number. SearchTimeline nests tweets at depth 12-14:
        #   data > search_by_raw_query > search_timeline > timeline >
        #   instructions[] > entries[] > content > itemContent > tweet_results >
        #   result > legacy
        # measured live against $CHEEMS: 22 tweet nodes at depths 12-14, and a
        # cap of 10 returned ZERO of them while reporting no error. That is the
        # silent-absence failure this module exists to avoid — every
        # session-gated social metric read MISSING because the walker gave up
        # two levels short of the data. 16 recovers all of them and saturates
        # (20 and 24 find nothing further).
        def walk(node: Any, depth: int = 0) -> None:
            if depth > 16 or len(found) > 600:
                return
            if isinstance(node, dict):
                legacy = node.get("legacy")
                if isinstance(legacy, dict) and "full_text" in legacy or ("id_str" in node or "rest_id" in node) and (
                    "full_text" in node or "text" in node
                ):
                    found.append(node)
                for value in node.values():
                    walk(value, depth + 1)
            elif isinstance(node, list):
                for item in node:
                    walk(item, depth + 1)

        walk(payload)
        return found

    @staticmethod
    def _user_fields(user_result: dict[str, Any]) -> dict[str, Any]:
        """Author attributes, read across both X user-object shapes.

        X removed `legacy` from the user object and split its contents into
        `core` (screen_name, name, created_at), `relationship_counts` (followers,
        following), `tweet_counts` (tweets) and `verification` (verified).

        Reading only the old path did not degrade to MISSING — it degraded to a
        confident falsehood. `rest_id` still resolved, so posts were created with
        author "unknown", and `mention_author_diversity` then saw one distinct
        author across 390 posts and reported maximum concentration — a shill
        fingerprint — at high confidence. Fabricated evidence of manipulation is
        strictly worse than absence, which is why both shapes are read here and
        why the caller falls back to the account id rather than to a constant.

        `fast_followers_count` has no home in the new shape at all, so
        `purchased_follower_signal` stays MISSING on this path. That is the
        correct outcome, not a gap to paper over.
        """
        def sub(key: str) -> dict[str, Any]:
            value = user_result.get(key)
            return value if isinstance(value, dict) else {}

        legacy = sub("legacy")
        core = sub("core")
        counts = sub("relationship_counts")
        tweets = sub("tweet_counts")
        verification = sub("verification")
        bio = sub("profile_bio")

        def pick(new: Any, old: Any) -> Any:
            return new if new is not None else old

        return {
            "screen_name": pick(core.get("screen_name"), legacy.get("screen_name")),
            "created_at": pick(core.get("created_at"), legacy.get("created_at")),
            "followers": pick(counts.get("followers"), legacy.get("followers_count")),
            "following": pick(counts.get("following"), legacy.get("friends_count")),
            "post_count": pick(tweets.get("tweets"), legacy.get("statuses_count")),
            "verified": pick(verification.get("verified"), legacy.get("verified")),
            "description": pick(bio.get("description"), legacy.get("description")),
            # Present only on the old shape; absent upstream means absent here.
            "fast_followers": legacy.get("fast_followers_count"),
            "normal_followers": legacy.get("normal_followers_count"),
        }

    def _parse_graphql_tweet(self, node: dict[str, Any]) -> SocialPost | None:
        raw_legacy = node.get("legacy")
        legacy = raw_legacy if isinstance(raw_legacy, dict) else node
        post_id = str(legacy.get("id_str") or node.get("rest_id") or "").strip()
        created = _iso(legacy.get("created_at"))
        if not post_id or created is None:
            return None

        user_result = (
            (node.get("core") or {}).get("user_results", {}).get("result", {})
            if isinstance(node.get("core"), dict)
            else {}
        )
        author = self._user_fields(user_result if isinstance(user_result, dict) else {})
        author_id = str(user_result.get("rest_id") or "") or None
        # Falling back to the account id keeps distinct authors distinct. A shared
        # placeholder collapses them into one, which reads downstream as a single
        # account posting everything — a manufactured shill signal.
        handle = author["screen_name"] or (f"id:{author_id}" if author_id else "unknown")
        text = str(legacy.get("full_text") or legacy.get("text") or "")
        views = node.get("views") or {}

        return SocialPost(
            platform=Platform.X,
            post_id=post_id,
            author=str(handle),
            author_id=author_id,
            as_of=created,
            observed_at=utcnow(),
            text=text,
            lang=legacy.get("lang"),
            url=f"https://x.com/{handle}/status/{post_id}",
            parent_id=str(legacy.get("in_reply_to_status_id_str") or "") or None,
            is_repost=bool(legacy.get("retweeted_status_result")),
            quotes=_int(legacy.get("quote_count")),
            likes=_int(legacy.get("favorite_count")),
            replies=_int(legacy.get("reply_count")),
            reposts=_int(legacy.get("retweet_count")),
            bookmarks=_int(legacy.get("bookmark_count")),
            views=_int(views.get("count")) if isinstance(views, dict) else None,
            author_followers=_int(author["followers"]),
            author_created_at=_iso(author["created_at"]),
            mentioned_tokens=extract_cashtags(text),
            source=f"{self.name}:graphql",
        )

    # -- enrichment --------------------------------------------------------- #

    async def _timeline_leg(self, handle: str, token: TokenRef, result: CollectionResult) -> list[SocialPost]:
        """The named account's own timeline: the only free source of follower
        counts and account ages, so its account object is kept even when the
        posts are later filtered out as off-topic."""
        posts, account = await self.profile_timeline(handle)
        if account is None:
            return posts
        result.accounts.append(account)
        if account.fast_follower_share is not None:
            result.raw.setdefault("fast_follower_share", {})[token.key] = (
                account.fast_follower_share
            )
        return posts

    async def _search_leg(self, symbol: str, result: CollectionResult) -> list[SocialPost]:
        posts = await self.search_via_browser(f"${symbol}" if symbol.isalnum() else symbol)
        if self._browser_failed:
            # Falling back to the syndication timeline is correct; doing it
            # silently is not. Without the browser there is no reply text and no
            # search breadth at all, so the social family reads thin — and a thin
            # reading reported as healthy gets attributed to the token instead of
            # to the collector.
            result.degraded = True
            result.raw["browser"] = "unavailable — syndication timeline only"
        return posts

    def _about_this_token(self, post: SocialPost, symbol: str) -> bool:
        if not symbol:
            return True
        if symbol.upper() in [t.upper() for t in post.mentioned_tokens]:
            return True
        return contains_address(post.text) or symbol.lower() in post.text.lower()

    async def enrich(self, tokens: Sequence[TokenRef]) -> CollectionResult:
        result = self._empty()
        handles: dict[str, str] = self.config.extra.get("handles", {})

        for token in list(tokens)[:8]:
            symbol = (token.symbol or "").strip()
            collected: list[SocialPost] = []

            handle = handles.get(token.key)
            if handle:
                collected.extend(await self._timeline_leg(handle, token, result))
            if symbol and len(symbol) >= 2:
                collected.extend(await self._search_leg(symbol, result))

            if not collected:
                result.degraded = True
                continue

            on_topic = [p for p in collected if self._about_this_token(p, symbol)]
            result.posts.extend(tag_posts(on_topic, token.key))

        return result


class RedditCollector(Collector):
    """Reddit via Arctic Shift, because Reddit's own JSON is closed to us.

    Verified 2026-07-25: `reddit.com/r/{sub}/new.json` returns HTTP 403 with a
    190 KB HTML interstitial to datacenter IPs. Arctic Shift mirrors the full
    108-field post schema, is unauthenticated, and answers from the same
    addresses that Reddit refuses.

    Reddit matters here because it is the cheapest genuinely off-platform signal:
    a ticker appearing in a general subreddit rather than only in the memecoin
    ones is a real propagation event, which is what
    `cross_platform_propagation_lag` is built to detect.
    """

    name = "reddit"
    description = "Reddit posts via the Arctic Shift mirror, with a direct fallback"
    can_discover = False
    can_enrich = True

    SUBREDDITS = (
        "solana",
        "CryptoMoonShots",
        "SatoshiStreetBets",
        "CryptoCurrency",
        "memecoins",
    )

    def default_headers(self) -> dict[str, str]:
        return {"User-Agent": self.settings.reddit_user_agent}

    async def health_check(self) -> bool:
        if not self.config.enabled:
            return False
        with contextlib.suppress(Exception):
            payload = await self.client.get_json(
                f"{ARCTIC_SHIFT}/posts/search",
                params={"subreddit": "solana", "limit": 1, "sort": "desc"},
                cache_ttl=0.0,
            )
            return isinstance(payload, dict) and "data" in payload
        return False

    def _parse(self, data: dict[str, Any]) -> SocialPost | None:
        post_id = data.get("id")
        created = _iso(data.get("created_utc"))
        if not post_id or created is None:
            return None
        text = f"{data.get('title') or ''} {data.get('selftext') or ''}".strip()
        url = data.get("url") or ""
        return SocialPost(
            platform=Platform.REDDIT,
            post_id=str(post_id),
            author=str(data.get("author") or "unknown"),
            author_id=data.get("author_fullname"),
            as_of=created,
            observed_at=utcnow(),
            text=text,
            url=f"{REDDIT_BASE}{data['permalink']}" if data.get("permalink") else None,
            likes=_int(data.get("score") or data.get("ups")),
            replies=_int(data.get("num_comments")),
            media_urls=[url] if url.endswith((".png", ".jpg", ".jpeg", ".gif")) else [],
            mentioned_tokens=extract_cashtags(text),
            source=f"{self.name}:arctic-shift",
        )

    async def enrich(self, tokens: Sequence[TokenRef]) -> CollectionResult:
        result = self._empty()
        for token in list(tokens)[:10]:
            symbol = (token.symbol or "").strip()
            # Short tickers produce an overwhelming false-positive rate in
            # free-text search. Not collecting is more honest than filtering after.
            if not symbol or len(symbol) < 3:
                continue
            try:
                payload = await self.client.get_json(
                    f"{ARCTIC_SHIFT}/posts/search",
                    params={"query": symbol, "limit": 50, "sort": "desc"},
                    cache_ttl=120.0,
                )
            except Exception as exc:
                result.degraded = True
                log.debug("reddit.search_failed", symbol=symbol, error=str(exc))
                continue

            rows = (payload or {}).get("data") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                post = self._parse(row)
                if post is None:
                    continue
                if symbol.lower() not in post.text.lower() and not contains_address(post.text):
                    continue
                if symbol.upper() not in post.mentioned_tokens:
                    post.mentioned_tokens.append(symbol.upper())
                result.posts.extend(tag_posts([post], token.key))
        return result


def _catalog_matches(catalog: Any, symbols: set[str]) -> list[int]:
    """Thread numbers whose subject or comment names a monitored ticker.

    The catalog is scanned rather than the search endpoint because 4chan has no
    search API; matching on the catalog blob is the only way to narrow 200-odd
    threads to the handful worth a request each.
    """
    matches: list[int] = []
    for page in catalog or []:
        if not isinstance(page, dict):
            continue
        for thread in page.get("threads") or []:
            if not isinstance(thread, dict):
                continue
            blob = f"{thread.get('sub') or ''} {thread.get('com') or ''}".upper()
            if thread.get("no") and any(sym in blob for sym in symbols):
                matches.append(int(thread["no"]))
    return matches


class FourChanBizCollector(Collector):
    """/biz/ via the read-only JSON API.

    Unfashionable and genuinely useful. It is an early and extremely toxic
    leading indicator: tickers surface there before they surface anywhere with a
    moderation team, the API is fully public with one documented rule (no more
    than one request per second), and every image carries a native MD5, which
    makes cross-surface image identity free rather than requiring a perceptual
    hash.

    The polling design follows the documented etiquette: `threads.json` is three
    fields per thread and cheap, so it is polled for `last_modified` changes and
    only changed threads are fetched.
    """

    name = "fourchan"
    description = "4chan /biz/ catalog and threads, with native image MD5s"
    can_discover = False
    can_enrich = True

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self.config.requests_per_minute = min(self.config.requests_per_minute, FOURCHAN_RPM)

    async def health_check(self) -> bool:
        if not self.config.enabled:
            return False
        with contextlib.suppress(Exception):
            payload = await self.client.get_json(
                f"{FOURCHAN_API}/biz/threads.json", cache_ttl=0.0
            )
            return isinstance(payload, list) and len(payload) > 0
        return False

    def _post_to_social(self, post: dict[str, Any], thread_no: int) -> SocialPost | None:
        number = post.get("no")
        created = _iso(post.get("time"))
        if not number or created is None:
            return None
        body = _strip_html(str(post.get("com") or ""))
        subject = _strip_html(str(post.get("sub") or ""))
        text = f"{subject} {body}".strip()

        media_urls: list[str] = []
        media_hashes: list[str] = []
        if post.get("tim") and post.get("ext"):
            media_urls.append(f"{FOURCHAN_CDN}/biz/{post['tim']}{post['ext']}")
            if post.get("md5"):
                # Native MD5: exact-duplicate detection across surfaces for free.
                # Namespaced, because it is not comparable with a perceptual hash
                # and `derivative_remix_depth` clusters in Hamming space — an
                # unlabelled MD5 in that column reads as a unique visual idea.
                media_hashes.append(exact_label(str(post["md5"])))

        return SocialPost(
            platform=Platform.FOURCHAN,
            post_id=str(number),
            # /biz/ is pseudonymous by default; the tripcode is the only stable
            # identity and is usually absent.
            author=str(post.get("trip") or post.get("name") or "Anonymous"),
            as_of=created,
            observed_at=utcnow(),
            text=text,
            url=f"https://boards.4chan.org/biz/thread/{thread_no}#p{number}",
            parent_id=str(thread_no) if number != thread_no else None,
            replies=_int(post.get("replies")),
            media_urls=media_urls,
            media_hashes=media_hashes,
            mentioned_tokens=extract_cashtags(text),
            source=self.name,
        )

    async def enrich(self, tokens: Sequence[TokenRef]) -> CollectionResult:
        """Scan the catalog for any monitored ticker, then read matching threads."""
        result = self._empty()
        symbols = {
            (t.symbol or "").upper().strip(): t
            for t in tokens
            if t.symbol and len(t.symbol.strip()) >= 3
        }
        if not symbols:
            return result

        try:
            catalog = await self.client.get_json(f"{FOURCHAN_API}/biz/catalog.json", cache_ttl=45.0)
        except Exception as exc:
            result.degraded = True
            log.debug("fourchan.catalog_failed", error=str(exc))
            return result

        # Bounded: the catalog is 200+ threads and the etiquette is 1 req/sec.
        for thread_no in _catalog_matches(catalog, set(symbols))[:8]:
            try:
                payload = await self.client.get_json(
                    f"{FOURCHAN_API}/biz/thread/{thread_no}.json", cache_ttl=60.0
                )
            except Exception as exc:
                result.degraded = True
                log.debug("fourchan.thread_failed", thread=thread_no, error=str(exc))
                continue
            for post in (payload or {}).get("posts") or []:
                if not isinstance(post, dict):
                    continue
                social = self._post_to_social(post, thread_no)
                if social is not None:
                    self._attribute_post(result, social, symbols)

        return result

    @staticmethod
    def _attribute_post(
        result: CollectionResult, social: SocialPost, symbols: dict[str, TokenRef]
    ) -> None:
        """Attach a thread post to a monitored token, or drop it.

        This board is scanned once for every ticker at a time, so a thread can
        mention several. `social_posts` is keyed (platform, post_id,
        observed_at) with token_key outside the key, so one post can only be
        attributed to one token: the first ticker matched wins, and the rest
        remain visible through `mentioned_tokens`. Fanning one post out to
        several tokens would need the key widened, which is a schema migration.
        """
        blob = social.text.upper()
        matched = [sym for sym in symbols if sym in blob]
        if not matched and not contains_address(social.text):
            return
        for sym in matched:
            if sym not in social.mentioned_tokens:
                social.mentioned_tokens.append(sym)

        owner = symbols.get(matched[0]) if matched else None
        if owner is not None:
            result.posts.extend(tag_posts([social], owner.key))
        else:
            # Matched only by mint address, so which token is not known here.
            # Left untagged deliberately rather than guessed: attributing it to
            # the wrong token would corrupt that token's social metrics, which
            # is worse than one lost post.
            result.posts.append(social)


class TelegramChannelCollector(Collector):
    """Public Telegram channels via the web preview.

    `t.me/s/{channel}` renders the last twenty messages of any public broadcast
    channel as plain HTML with no login, no API key and no phone number. That
    matters because the Bot API is structurally useless here — a bot only
    receives channel posts if it is an administrator, which no call channel will
    ever grant — and MTProto requires binding a real phone number to an account
    that will be banned for exactly this behaviour.

    Call channels front-run retail by minutes. Reading them at the moment they
    post is one of the few genuinely timing-sensitive edges available.
    """

    name = "telegram"
    description = "Public Telegram channel messages via the t.me web preview"
    can_discover = False
    can_enrich = True

    async def health_check(self) -> bool:
        if not self.config.enabled:
            return False
        with contextlib.suppress(Exception):
            html = await self.client.get_text(f"{TELEGRAM_PREVIEW}/durov", cache_ttl=0.0)
            return "tgme_widget_message" in (html or "")
        return False

    async def channel_messages(self, channel: str, limit: int = 20) -> list[SocialPost]:
        channel = channel.strip().lstrip("@").rstrip("/").split("/")[-1]
        if not channel:
            return []
        url = f"{TELEGRAM_PREVIEW}/{channel}"
        try:
            html = await self.client.get_text(url, cache_ttl=45.0)
            _require_body(html, url)
        except Exception as exc:
            log.debug("telegram.fetch_failed", channel=channel, error=str(exc))
            return []

        posts: list[SocialPost] = []
        for match in _TG_MESSAGE_RE.finditer(html):
            data_post = match.group(1)
            block = match.group(2)

            time_match = _TG_TIME_RE.search(block)
            created = _iso(time_match.group(1)) if time_match else None
            if created is None:
                continue

            text_match = _TG_TEXT_RE.search(block)
            text = _strip_html(text_match.group(1)) if text_match else ""
            views_match = _TG_VIEWS_RE.search(block)

            posts.append(
                SocialPost(
                    platform=Platform.TELEGRAM,
                    post_id=data_post,
                    author=channel,
                    as_of=created,
                    observed_at=utcnow(),
                    text=text,
                    url=f"https://t.me/{data_post}",
                    views=_abbrev_int(views_match.group(1)) if views_match else None,
                    mentioned_tokens=extract_cashtags(text),
                    source=self.name,
                )
            )
            if len(posts) >= limit:
                break
        return posts

    async def enrich(self, tokens: Sequence[TokenRef]) -> CollectionResult:
        """Read each token's own channel, plus any curated call channels."""
        result = self._empty()
        channels: dict[str, str] = self.config.extra.get("channels", {})
        watchlist: list[str] = self.config.extra.get("call_channels", [])

        for token in list(tokens)[:8]:
            channel = channels.get(token.key)
            if not channel:
                continue
            posts = await self.channel_messages(channel)
            if not posts:
                result.degraded = True
                continue
            for post in posts:
                if token.symbol and token.symbol.upper() not in post.mentioned_tokens:
                    post.mentioned_tokens.append(token.symbol.upper())
                result.posts.extend(tag_posts([post], token.key))

        # Call channels are token-agnostic: read them once and let the caller
        # match mints out of the message text.
        for channel in watchlist[:6]:
            posts = await self.channel_messages(channel)
            result.posts.extend(p for p in posts if contains_address(p.text) or p.mentioned_tokens)

        return result


class PumpFunChatCollector(Collector):
    """pump.fun's own comment stream, read through the browser.

    The replies under a coin on pump.fun are the highest-signal comment corpus
    available for a token that is minutes old, because they come from people
    already looking at the chart. The REST endpoint that used to serve them is
    gone; comments now arrive over a Socket.IO connection the coin page opens, so
    the browser path reads them off the wire as the page receives them.
    """

    name = "pumpfun_chat"
    description = "pump.fun coin-page comments captured via web-use"
    can_discover = False
    can_enrich = True

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self._browser_failed = False

    async def health_check(self) -> bool:
        return self.config.enabled and not self._browser_failed

    async def enrich(self, tokens: Sequence[TokenRef]) -> CollectionResult:
        result = self._empty()
        if self._browser_failed:
            result.degraded = True
            return result

        driver = get_driver(self.settings.browser)
        for token in list(tokens)[:6]:
            try:
                page = await driver.visit(
                    f"https://pump.fun/coin/{token.mint}",
                    surface=self.name,
                    capture_patterns=[r"(livechat|replies|messages|socket\.io)"],
                    wait_ms=6000,
                    scrolls=1,
                    requests_per_minute=self.config.requests_per_minute,
                )
            except BrowserUnavailableError as exc:
                self._browser_failed = True
                result.degraded = True
                log.info("pumpfun_chat.browser_unavailable", error=str(exc))
                return result
            except Exception as exc:
                result.degraded = True
                log.debug("pumpfun_chat.visit_failed", mint=token.mint, error=str(exc))
                continue

            for body in page.json_matching(r"(livechat|replies|messages)"):
                result.posts.extend(tag_posts(self._parse_messages(body, token), token.key))

        return result

    def _parse_messages(self, body: Any, token: TokenRef) -> list[SocialPost]:
        out: list[SocialPost] = []

        def walk(node: Any, depth: int = 0) -> None:
            if depth > 6 or len(out) > 300:
                return
            if isinstance(node, dict):
                text = node.get("message") or node.get("text") or node.get("body")
                author = node.get("username") or node.get("user") or node.get("userAddress")
                created = _iso(
                    node.get("timestamp") or node.get("created_at") or node.get("createdAt")
                )
                identifier = node.get("id") or node.get("messageId")
                if text and author and created is not None:
                    out.append(
                        SocialPost(
                            platform=Platform.PUMPFUN_CHAT,
                            post_id=str(identifier or f"{author}:{created.timestamp():.0f}"),
                            author=str(author),
                            as_of=created,
                            observed_at=utcnow(),
                            text=str(text),
                            parent_id=f"coin:{token.mint}",
                            mentioned_tokens=[token.symbol] if token.symbol else [],
                            source=self.name,
                        )
                    )
                for value in node.values():
                    walk(value, depth + 1)
            elif isinstance(node, list):
                for item in node:
                    walk(item, depth + 1)

        walk(body)
        return out


__all__ = [
    "ARCTIC_SHIFT",
    "FOURCHAN_API",
    "FOURCHAN_CDN",
    "REDDIT_BASE",
    "TELEGRAM_PREVIEW",
    "X_DEAD_ENDPOINTS",
    "X_PUBLIC_SEARCH_DEADLINE_SECONDS",
    "X_SEARCH_DEADLINE_SECONDS",
    "X_SEARCH_IDLE_SCROLLS",
    "X_SEARCH_MAX_SCROLLS",
    "X_SEARCH_PATTERN",
    "X_SEARCH_POST_TARGET",
    "X_TIMELINE_HOST",
    "X_TWEET_HOST",
    "EmptySuccessError",
    "FourChanBizCollector",
    "PumpFunChatCollector",
    "RedditCollector",
    "TelegramChannelCollector",
    "XCollector",
    "x_tweet_token",
]
