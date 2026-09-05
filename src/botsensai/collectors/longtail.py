"""Instagram and TikTok — the two platforms a Solana token has to escape to.

`cross_platform_propagation_lag` asks one question: has this token been talked
about anywhere its team did not aim at? Crypto Twitter proves nothing, because
that is the room the launch was fired into. TikTok and Instagram are the first
venues that are not.

Both are also the two hardest surfaces in the system to read, and this module is
mostly a record of *how* hard, measured live on 2026-08-04 from a headless
anonymous browser rather than assumed from documentation:

**Instagram is shut to a stranger, in two different disguises.**
`instagram.com/explore/tags/memecoin/` redirects to `/accounts/login/`. But
`/explore/tags/wif/`, in the same run, redirects instead to
`/popular/wif/?utm_source=explore_tag` — HTTP 200, 15 KB of rendered text, zero
captured JSON, and the prose is an encyclopedia entry about **Wi-Fi**. Both
shapes are live simultaneously and neither carries a single post.

The second is the more dangerous by a distance, because it does not look like a
failure. `api/v1/tags/web_info/` behaves the same way, answering an anonymous
caller with **HTTP 200, `content-type: text/html`, 605 KB** of login shell.
These are the empty-success trap in new costumes: not a 404, not a 429, a
success carrying a body of entirely the wrong kind. Anything that reads one as
"no posts for this tag" writes a permanent silent zero into the one metric that
scores absence bearishly, and anything that ever falls back to rendered text
files an article on wireless networking as social evidence for a memecoin. So
the reachability probe here demands JSON, the collector demands captured
bodies, and `is_signed_out` knows both disguises by name.

**TikTok gates everything token-scoped.** `/tag/{tag}` gets HTTP 403 from
`api/challenge/detail/` and therefore never asks for an item list at all —
identical under a real Chrome user-agent and under Playwright's own, so this is
not user-agent sniffing and no amount of stealth fixes it. `/search` is gated by
TikTok's own admission: the page's rehydration blob carries a
`searchVideoForLoggedin` flag. The generic `/explore` feed pays out exactly one
200 per fresh browser context (7 items) and then 403s — 50 consecutive times in
one visit — and is not token-scoped anyway.

**One TikTok path does work, and it is enough to matter.** `oembed` is
unauthenticated and healthy: 20 of 20 consecutive requests returned 200 in 5.2 s
with the full caption and hashtags, and a bogus video id returns a clean 400
rather than a hollow 200. It resolves a *known video URL*, which sounds like a
chicken-and-egg problem until you notice that TikTok links are already flowing
through the sweep — people post them in the X, Telegram, 4chan and pump.fun-chat
text this system already collects. `Pipeline._follow_offplatform_links` is where
that loop closes.

**TikTok video ids are snowflakes**, verified: `id >> 32` is unix seconds
(`6718335390845095173` → 2019-07-27, matching that video's real age). A post's
creation time therefore comes out of its URL with no request at all, which is
what makes a lag computable from a link alone.

The design rule throughout: **degradation is the normal path, and it must be
loud.** A collector here that returns an empty result without setting
`degraded=True` hands `cross_platform_propagation_lag` a fabricated bearish
reading for every token, forever, and nothing downstream can tell the difference.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from botsensai.collectors.base import CollectionResult, Collector, tag_posts
from botsensai.collectors.browser import BrowserUnavailableError, get_driver
from botsensai.config import Settings
from botsensai.models import Platform, SocialPost, TokenRef, utcnow
from botsensai.util.http import PacedClient
from botsensai.util.logging import get_logger
from botsensai.util.text import extract_cashtags, extract_hashtags

log = get_logger(__name__)

# --- Instagram ------------------------------------------------------------- #
INSTAGRAM_BASE = "https://www.instagram.com"
#: The public web app id the site sends on its own XHRs. Not a credential and
#: not tied to an account; without it the JSON endpoint answers in HTML.
INSTAGRAM_APP_ID = "936619743392459"
#: Operations worth capturing off a hashtag page, matched as URL substrings
#: because Instagram versions the path and not the operation.
INSTAGRAM_CAPTURE = r"(tags/web_info|graphql/query|api/v1/tags)"
#: A redirect here is the whole story: anonymous access is not rate-limited, it
#: is refused.
INSTAGRAM_LOGIN_PATH = "/accounts/login"
#: The *other* signed-out answer, and the dangerous one. `/explore/tags/wif/`
#: does not redirect to the login form at all — it lands on
#: `/popular/wif/?utm_source=explore_tag`, an SEO page carrying 15 KB of
#: encyclopedia prose about **Wi-Fi**, with no posts and no captured JSON. A
#: parser that ever falls back to rendered text would file an article about
#: wireless networking as social evidence for the token $WIF. Measured
#: 2026-08-04; `/explore/tags/memecoin/` on the same run took the login
#: redirect instead, so both shapes are live simultaneously.
INSTAGRAM_POPULAR_PATH = "/popular/"

# --- TikTok ---------------------------------------------------------------- #
TIKTOK_BASE = "https://www.tiktok.com"
#: Measured healthy: 20/20 consecutive 200s in 5.2s, bogus id → clean 400.
TIKTOK_OEMBED = "https://www.tiktok.com/oembed"
#: What a search page fetches when it is willing to answer. Captured rather than
#: called directly: the request is signed by the page.
TIKTOK_CAPTURE = r"(item_list|api/search/(general|item)|api/challenge)"

#: Measured 2026-08-04: 20 consecutive oembed calls in 5.2s (~230/min) with no
#: throttling of any kind. 60/min is a fifth of what held, which is the margin
#: this system keeps on every endpoint it has only burst-tested. It is the only
#: measured rate here — Instagram's clamp lives with every other surface's in
#: `Settings._defaults_and_guards`, and is a small number rather than a measured
#: one because a platform that will not answer has no throughput to budget for.
TIKTOK_OEMBED_RPM = 60

#: Anything longer is spent discovering a login wall we have already measured.
BROWSER_WAIT_MS = 6000
SIGNED_OUT_ERROR = (
    "signed out: Instagram served a logged-out page (login wall or /popular/ SEO "
    "landing), not a hashtag feed"
)
SEARCH_GATED_ERROR = (
    "search results are login-gated (api item_list answers 403 anonymously, "
    "measured 2026-08-04)"
)

_TIKTOK_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|vm\.|vt\.)?tiktok\.com/(?:@[\w.\-]+/(?:video|photo)/(\d+)|t/(\w+))",
    re.IGNORECASE,
)
_TIKTOK_ID_RE = re.compile(r"/(?:video|photo)/(\d+)")
#: TikTok ids below this are not plausible snowflakes; the epoch shift would put
#: them before TikTok existed. Guards against reading a short numeric path id as
#: a timestamp and inventing a 1970 post.
_TIKTOK_MIN_ID = 1 << 32


def tiktok_video_id(url: str) -> str | None:
    """The numeric id out of a TikTok video URL, if it carries one.

    Short `vm.tiktok.com/t/{code}` links do not: they are redirects, and
    resolving one costs a request. Callers get None and can decide whether that
    request is worth making.
    """
    match = _TIKTOK_ID_RE.search(url or "")
    return match.group(1) if match else None


def tiktok_created_at(video_id: str) -> datetime | None:
    """Creation time decoded from a TikTok id.

    TikTok ids are snowflakes: the top 32 bits are unix seconds. Verified
    against a known 2019 video, whose id decodes to 2019-07-27. This is the only
    reason a link found in someone else's post can be placed on a timeline
    without asking TikTok anything — and `cross_platform_propagation_lag` is
    made entirely of timestamps, so a post without one is not evidence.
    """
    try:
        numeric = int(video_id)
    except (TypeError, ValueError):
        return None
    if numeric < _TIKTOK_MIN_ID:
        return None
    with contextlib.suppress(OSError, OverflowError, ValueError):
        return datetime.fromtimestamp(numeric >> 32, tz=UTC)
    return None


def tiktok_links(text: str) -> list[str]:
    """Every TikTok video link in a blob of text, deduped, order preserved."""
    seen: dict[str, None] = {}
    for match in _TIKTOK_URL_RE.finditer(text or ""):
        seen.setdefault(match.group(0).rstrip(").,\"'"), None)
    return list(seen)


def hashtag_for(token: TokenRef) -> str | None:
    """The tag a token would plausibly be posted under.

    Symbols are the only thing both platforms index by. A symbol that survives
    stripping to fewer than two characters is not a searchable tag, it is noise
    that would return the whole platform.
    """
    symbol = (token.symbol or "").strip().lstrip("$")
    cleaned = re.sub(r"[^A-Za-z0-9]", "", symbol).lower()
    return cleaned if len(cleaned) >= 2 else None


#: Text a signed-out Instagram page renders in place of a feed. From the
#: measured bodies: the login form's "Log into Instagram / … / Forgot password?"
#: and, on the `/popular/` landing, the signed-out chrome's "Log In / Sign Up".
_SIGNED_OUT_TEXT_MARKERS = (
    "log into instagram",
    "forgot password",
    "log in with facebook",
    "log in\nsign up",
)


def is_signed_out(page: Any) -> bool:
    """Whether Instagram answered as it answers a stranger.

    Three signals, none of them sufficient alone, all of them measured:

    * the login redirect, `/accounts/login/`;
    * the `/popular/` SEO landing, which is a *200 with plausible prose* and no
      posts — strictly more dangerous than the login form, because it looks like
      content;
    * the signed-out chrome in the rendered text, which is the fallback for when
      the URL admits nothing. Instagram performs its redirect in its own
      JavaScript after hydration, so whether the URL has moved by the time it is
      read depends on a race — measured landing on `/accounts/login/` for one
      tag and on `/popular/` for another in the same run.

    Getting this wrong does not lose data, since there is none to lose either
    way. It loses the *reason*, and a surface whose failures are all logged as
    "fetched nothing" is a surface nobody can tell has broken.
    """
    final_url = getattr(page, "final_url", "") or ""
    if INSTAGRAM_LOGIN_PATH in final_url or INSTAGRAM_POPULAR_PATH in final_url:
        return True
    text = (getattr(page, "text", "") or "").lower()
    return any(marker in text for marker in _SIGNED_OUT_TEXT_MARKERS)


def _walk_media(payload: Any, out: list[dict[str, Any]], depth: int = 0) -> None:
    """Collect every dict that looks like an Instagram media node.

    A structural walk rather than a key path, for the same reason
    `XCollector._walk_for_tweets` is one: Instagram serves this data through at
    least two envelopes (`api/v1/tags/web_info` and a GraphQL query whose
    top-level key carries a version in its name), reshapes them without notice,
    and nests the media several layers inside `sections → layout_content →
    medias → media`. Matching on the *shape of a post* survives all of that;
    matching on a path survives until the next deploy.

    The depth limit is 12 because the GraphQL envelope really is that deep:
    `data → xdt_api__v1__tags__web_info → recent → sections[] → layout_content →
    medias[] → media` puts a post nine levels down, and every list index counts
    as one. A limit set to the REST envelope's depth silently parsed one shape
    and returned nothing at all for the other.
    """
    if depth > 12 or len(out) > 400:
        return
    if isinstance(payload, dict):
        if _looks_like_media(payload):
            out.append(payload)
        for value in payload.values():
            _walk_media(value, out, depth + 1)
    elif isinstance(payload, list):
        for item in payload:
            _walk_media(item, out, depth + 1)


def _looks_like_media(node: dict[str, Any]) -> bool:
    """A media node is one with an id, a timestamp and an owner.

    All three tests earn their place. The timestamp looks redundant — a node
    without one is rejected again by `_parse_media` — but this predicate also
    decides what fills the walker's collection cap, and an Instagram response
    carries hundreds of owner-bearing nodes that are not posts: suggested
    accounts, related-tag rails, commenter stubs. Admitting those exhausts the
    cap on non-posts and the real media at the end of the payload is never
    reached, which reads downstream as a hashtag nobody posted under.
    """
    has_id = bool(node.get("id") or node.get("pk") or node.get("code") or node.get("shortcode"))
    has_time = node.get("taken_at") is not None or node.get("taken_at_timestamp") is not None
    has_owner = isinstance(node.get("user"), dict) or isinstance(node.get("owner"), dict)
    return bool(has_id and has_time and has_owner)


def _media_caption(node: dict[str, Any]) -> str:
    caption = node.get("caption")
    if isinstance(caption, dict):
        return str(caption.get("text") or "")
    if isinstance(caption, str):
        return caption
    edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
    if edges and isinstance(edges[0], dict):
        return str((edges[0].get("node") or {}).get("text") or "")
    return ""


def _media_owner(node: dict[str, Any]) -> tuple[str, str | None]:
    owner = node.get("user") if isinstance(node.get("user"), dict) else node.get("owner")
    if not isinstance(owner, dict):
        return "unknown", None
    name = owner.get("username") or owner.get("full_name") or "unknown"
    return str(name), (str(owner["id"]) if owner.get("id") else None)


def _sub_dict(node: dict[str, Any], key: str) -> dict[str, Any]:
    """A nested object, or an empty one. These payloads null out sub-objects
    rather than omitting them, so `node.get(key) or {}` is not enough."""
    value = node.get(key)
    return value if isinstance(value, dict) else {}


def _count(node: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = node.get(name)
        if isinstance(value, dict):
            value = value.get("count")
        if isinstance(value, (int, float)):
            return int(value)
    return None


class InstagramCollector(Collector):
    """Public Instagram hashtag pages, read through the browser.

    Measured anonymously on 2026-08-04: every hashtag URL ends at
    `/accounts/login/` with zero captured JSON, and the JSON endpoint answers in
    HTML. This collector is therefore written to be *correct when it fails* —
    the interesting behaviour is not the parse, it is that a login wall is
    reported as a degraded surface with a name, never as a token nobody posted
    about.

    It becomes a real data path the moment `browser.user_data_dir` points at a
    logged-in profile, which is the same opt-in mechanism `XSessionSettings`
    documents for X: the operator logs in once by hand, and this collector then
    sees exactly what that operator can see and nothing more.
    """

    name = "instagram"
    description = "Public Instagram hashtag pages via web-use (session-gated)"
    can_discover = False
    can_enrich = True

    #: Hashtag pages are expensive and near-certain to be walled; there is no
    #: point paying for more than a handful of tokens per sweep.
    max_tokens_per_sweep = 3

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self._browser_failed = False
        #: Set once Instagram has answered as it answers a stranger. See the
        #: note in `enrich`: this is a budget latch, not a correctness one.
        self._signed_out = False

    def default_headers(self) -> dict[str, str]:
        return {
            "x-ig-app-id": INSTAGRAM_APP_ID,
            "accept": "application/json",
        }

    async def health_check(self) -> bool:
        """Reachable means *answered in JSON*, not merely answered.

        The anonymous 200 here is 605 KB of login-page HTML. Reporting that as
        a reachable surface is how a dead platform stays green on the dashboard
        for weeks.
        """
        if not self.config.enabled:
            return False
        url = f"{INSTAGRAM_BASE}/api/v1/tags/web_info/?tag_name=solana"
        with contextlib.suppress(Exception):
            payload = await self.client.get_json(url, cache_ttl=0.0)
            return isinstance(payload, dict) and bool(payload.get("data"))
        return False

    def _parse_media(self, node: dict[str, Any], tag: str) -> SocialPost | None:
        raw_time: Any = node.get("taken_at") or node.get("taken_at_timestamp")
        if raw_time is None:
            return None
        try:
            created = datetime.fromtimestamp(float(raw_time), tz=UTC)
        except (TypeError, ValueError, OSError, OverflowError):
            return None

        author, author_id = _media_owner(node)
        text = _media_caption(node)
        code = node.get("code") or node.get("shortcode")
        post_id = str(node.get("id") or node.get("pk") or code)
        return SocialPost(
            platform=Platform.INSTAGRAM,
            post_id=post_id,
            author=author,
            author_id=author_id,
            as_of=created,
            observed_at=utcnow(),
            text=text,
            url=f"{INSTAGRAM_BASE}/p/{code}/" if code else None,
            likes=_count(node, "like_count", "edge_liked_by", "edge_media_preview_like"),
            replies=_count(node, "comment_count", "edge_media_to_comment"),
            views=_count(node, "play_count", "view_count", "video_view_count"),
            mentioned_tokens=_mentions(text, tag),
            source=self.name,
        )

    def parse_bodies(self, bodies: Iterable[Any], tag: str) -> list[SocialPost]:
        """Every media node in every captured body, deduped by post id."""
        nodes: list[dict[str, Any]] = []
        for body in bodies:
            _walk_media(body, nodes)
        posts: dict[str, SocialPost] = {}
        for node in nodes:
            post = self._parse_media(node, tag)
            if post is not None:
                posts.setdefault(post.post_id, post)
        return list(posts.values())

    @property
    def has_session(self) -> bool:
        """Whether a logged-in profile is configured.

        Without one this surface is not slow or throttled, it is *shut*: the
        measurement is a redirect to `/accounts/login/` on every URL. Spending
        ten seconds of browser time per token to rediscover that every sweep
        would displace collection that works, so the anonymous case degrades
        without opening a page at all.
        """
        return bool(self.settings.browser.user_data_dir)

    async def _harvest_tag(self, tag: str, result: CollectionResult) -> list[SocialPost]:
        """One hashtag page. Returns posts; records why there were none."""
        driver = get_driver(self.settings.browser)
        page = await driver.visit(
            f"{INSTAGRAM_BASE}/explore/tags/{tag}/",
            surface=self.name,
            capture_patterns=[INSTAGRAM_CAPTURE],
            wait_ms=BROWSER_WAIT_MS,
            scrolls=2,
            # The rendered text is worth one `inner_text` call here, because a
            # signed-out page does not always announce itself in the URL — see
            # `is_signed_out`.
            extract_text=True,
            requests_per_minute=self.config.requests_per_minute,
        )
        if is_signed_out(page):
            self._signed_out = True
            result.degraded = True
            result.error = result.error or SIGNED_OUT_ERROR
            log.info("instagram.signed_out", tag=tag, final_url=page.final_url)
            return []

        bodies = page.json_matching(INSTAGRAM_CAPTURE)
        if not bodies:
            # Reached the page and it fetched nothing we can read. That is a
            # failure of this surface, not evidence about the token.
            result.degraded = True
            result.error = result.error or f"no readable payload on /explore/tags/{tag}/"
            return []
        return self.parse_bodies(bodies, tag)

    async def enrich(self, tokens: Sequence[TokenRef]) -> CollectionResult:
        result = self._empty()
        if self._browser_failed:
            result.degraded = True
            result.error = "browser unavailable"
            return result
        if not self.has_session:
            result.degraded = True
            result.error = (
                "no browser session configured; anonymous Instagram is signed out on "
                "every hashtag URL (measured 2026-08-04). Set browser.user_data_dir to "
                "a logged-in profile."
            )
            return result
        if self._signed_out:
            # A configured profile that Instagram does not recognise costs the
            # same ten seconds per token as no profile at all, and costs it
            # again on every sweep. Learning it once is the difference between
            # thirty seconds of a sweep and thirty seconds of every sweep. The
            # latch is process-lifetime: logging the profile back in means
            # restarting the daemon, which is already true of the X session.
            result.degraded = True
            result.error = f"{SIGNED_OUT_ERROR} (latched; restart after signing the profile in)"
            return result

        for token in list(tokens)[: self.max_tokens_per_sweep]:
            tag = hashtag_for(token)
            if not tag:
                continue
            try:
                posts = await self._harvest_tag(tag, result)
            except BrowserUnavailableError as exc:
                self._browser_failed = True
                result.degraded = True
                result.error = f"browser unavailable: {exc}"
                return result
            except Exception as exc:
                result.degraded = True
                result.error = result.error or f"{type(exc).__name__}: {exc}"
                log.debug("instagram.visit_failed", tag=tag, error=str(exc))
                continue
            result.posts.extend(tag_posts(posts, token.key))
        return result


class TikTokCollector(Collector):
    """TikTok through the one door that is open, plus the ones that are not.

    The search page is attempted because the plan asks for it and because a
    session or a future policy change would make it pay; it is measured shut and
    degrades by name when it is. The path that actually produces posts is
    `resolve_video`, which turns a TikTok link someone else posted into a dated,
    attributed `SocialPost` — the captions arrive complete, and the timestamp
    comes out of the video id rather than out of a second request.
    """

    name = "tiktok"
    description = "TikTok captions via oembed, with a search-page attempt"
    can_discover = False
    can_enrich = True

    #: Search is measured login-gated, so this is a probe budget, not a harvest.
    max_tokens_per_sweep = 2
    #: Ceiling on links resolved for one token in one sweep. A post carrying
    #: forty links is a spam wall, not forty pieces of evidence.
    max_links_per_token = 6

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self._browser_failed = False
        #: Set once the search page has answered without an item list. Same
        #: budget argument as Instagram's `_signed_out`: nine seconds per token
        #: is worth paying once to find out, and not worth paying every sweep.
        self._gated = False
        self._oembed: PacedClient | None = None

    @property
    def oembed(self) -> PacedClient:
        """Its own client: oembed took 20/20 at ~230/min while every browser
        path on this host is 403ing, so the two must not share a bucket or a
        breaker."""
        if self._oembed is None:
            self._oembed = PacedClient(
                f"{self.name}_oembed",
                requests_per_minute=min(self.config.requests_per_minute, TIKTOK_OEMBED_RPM),
                max_concurrency=self.config.max_concurrency,
                timeout=self.config.timeout_seconds,
                max_retries=self.config.max_retries,
                cache_ttl=600.0,
            )
        return self._oembed

    async def aclose(self) -> None:
        await super().aclose()
        if self._oembed is not None:
            await self._oembed.aclose()
            self._oembed = None

    async def health_check(self) -> bool:
        """oembed against a video that has existed since 2019.

        A stable target on purpose: a probe pointed at a fresh video reports the
        surface down the day that video is deleted, and this is the one TikTok
        path measured to work.
        """
        if not self.config.enabled:
            return False
        with contextlib.suppress(Exception):
            payload = await self._oembed_json(
                f"{TIKTOK_BASE}/@scout2015/video/6718335390845095173"
            )
            return bool(payload and payload.get("title"))
        return False

    async def _oembed_json(self, video_url: str) -> dict[str, Any] | None:
        payload = await self.oembed.get_json(
            TIKTOK_OEMBED, params={"url": video_url}, cache_ttl=600.0
        )
        return payload if isinstance(payload, dict) else None

    async def resolve_video(self, video_url: str, token: TokenRef | None = None) -> SocialPost | None:
        """One TikTok link → one dated, attributed post.

        Returns None for a link this cannot place in time. That is deliberate:
        a post with a guessed timestamp is worse than no post at all in a metric
        whose entire output is a time difference.
        """
        video_id = tiktok_video_id(video_url)
        if not video_id:
            return None
        created = tiktok_created_at(video_id)
        if created is None:
            return None

        try:
            payload = await self._oembed_json(video_url)
        except Exception as exc:
            log.debug("tiktok.oembed_failed", url=video_url, error=str(exc))
            return None
        if not payload or not payload.get("title"):
            return None

        caption = str(payload.get("title") or "")
        author = str(payload.get("author_unique_id") or payload.get("author_name") or "unknown")
        return SocialPost(
            platform=Platform.TIKTOK,
            post_id=str(payload.get("embed_product_id") or video_id),
            token_key=token.key if token else None,
            author=author,
            as_of=created,
            observed_at=utcnow(),
            text=caption,
            url=video_url,
            media_urls=[str(payload["thumbnail_url"])] if payload.get("thumbnail_url") else [],
            mentioned_tokens=_mentions(caption, None),
            source=self.name,
        )

    async def resolve_links(
        self, urls: Sequence[str], token: TokenRef | None = None
    ) -> list[SocialPost]:
        """Resolve a batch of links, dropping the ones that will not resolve."""
        posts: list[SocialPost] = []
        for url in list(urls)[: self.max_links_per_token]:
            post = await self.resolve_video(url, token)
            if post is not None:
                posts.append(post)
        return posts

    async def _search_page(self, symbol: str, result: CollectionResult) -> list[Any]:
        """Attempt the public search page. Measured login-gated; degrades loudly."""
        driver = get_driver(self.settings.browser)
        page = await driver.visit(
            f"{TIKTOK_BASE}/search?q=%24{symbol}",
            surface=self.name,
            capture_patterns=[TIKTOK_CAPTURE],
            wait_ms=BROWSER_WAIT_MS,
            scrolls=2,
            extract_text=False,
            requests_per_minute=self.config.requests_per_minute,
        )
        bodies = [b for b in page.json_matching(TIKTOK_CAPTURE) if _has_items(b)]
        if not bodies:
            self._gated = True
            result.degraded = True
            result.error = result.error or SEARCH_GATED_ERROR
        return bodies

    def parse_bodies(self, bodies: Iterable[Any]) -> list[SocialPost]:
        """Item lists out of whatever envelope they arrived in."""
        posts: dict[str, SocialPost] = {}
        for body in bodies:
            for item in _walk_items(body):
                post = self._parse_item(item)
                if post is not None:
                    posts.setdefault(post.post_id, post)
        return list(posts.values())

    def _parse_item(self, item: dict[str, Any]) -> SocialPost | None:
        item_id = str(item.get("id") or "")
        created = _iso_or_snowflake(item.get("createTime"), item_id)
        if not item_id or created is None:
            return None
        author: dict[str, Any] = _sub_dict(item, "author")
        stats: dict[str, Any] = _sub_dict(item, "stats")
        handle = str(author.get("uniqueId") or author.get("nickname") or "unknown")
        caption = str(item.get("desc") or "")
        return SocialPost(
            platform=Platform.TIKTOK,
            post_id=item_id,
            author=handle,
            author_id=str(author["id"]) if author.get("id") else None,
            as_of=created,
            observed_at=utcnow(),
            text=caption,
            url=f"{TIKTOK_BASE}/@{handle}/video/{item_id}",
            likes=_count(stats, "diggCount"),
            replies=_count(stats, "commentCount"),
            reposts=_count(stats, "shareCount"),
            views=_count(stats, "playCount"),
            mentioned_tokens=_mentions(caption, None),
            source=self.name,
        )

    @property
    def search_enabled(self) -> bool:
        """Whether to attempt the search page at all.

        Measured shut for an anonymous visitor, and rediscovering that costs
        about nine seconds of browser time per token. A configured session is
        the only condition under which the attempt can pay, so it is the
        condition under which the attempt is made. `extra.search` overrides it
        for anyone who wants to re-measure.
        """
        override = self.config.extra.get("search")
        if override is not None:
            return bool(override)
        return bool(self.settings.browser.user_data_dir)

    async def enrich(self, tokens: Sequence[TokenRef]) -> CollectionResult:
        """Curated links first — they work — then the search page attempt."""
        result = self._empty()
        curated: dict[str, list[str]] = self.config.extra.get("videos", {})

        for token in list(tokens):
            links = curated.get(token.key) or []
            if links:
                result.posts.extend(await self.resolve_links(links, token))

        if self.search_enabled and not self._gated:
            await self._search_tokens(tokens, result)
        elif not result.posts:
            # Nothing was read from TikTok for these tokens. Saying so is the
            # difference between "no one posted" and "we did not look", and
            # `cross_platform_propagation_lag` scores the first of those
            # bearishly.
            result.degraded = True
            result.error = (
                f"{SEARCH_GATED_ERROR}; search not attempted, only linked videos resolved"
            )
        return result

    async def _search_tokens(self, tokens: Sequence[TokenRef], result: CollectionResult) -> None:
        for token in list(tokens)[: self.max_tokens_per_sweep]:
            tag = hashtag_for(token)
            if not tag or self._browser_failed:
                continue
            try:
                bodies = await self._search_page(tag, result)
            except BrowserUnavailableError as exc:
                self._browser_failed = True
                result.degraded = True
                result.error = result.error or f"browser unavailable: {exc}"
                return
            except Exception as exc:
                result.degraded = True
                result.error = result.error or f"{type(exc).__name__}: {exc}"
                log.debug("tiktok.search_failed", tag=tag, error=str(exc))
                continue
            result.posts.extend(tag_posts(self.parse_bodies(bodies), token.key))


def _has_items(body: Any) -> bool:
    """Whether a captured body carries an item list rather than a status echo.

    TikTok's search page fetches several endpoints that answer 200 with nothing
    but `status_code` and a log id. Counting those as a payload is how a
    login-gated surface reports itself healthy.
    """
    if not isinstance(body, dict):
        return False
    for key in ("itemList", "item_list", "data"):
        value = body.get(key)
        if isinstance(value, list) and value:
            return True
    return False


def _walk_items(payload: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Every TikTok item node in a captured body, however it was wrapped."""
    out: list[dict[str, Any]] = []
    if depth > 6 or not isinstance(payload, (dict, list)):
        return out
    if isinstance(payload, dict):
        for key in ("itemList", "item_list"):
            value = payload.get(key)
            if isinstance(value, list):
                out.extend(item for item in value if isinstance(item, dict))
        if isinstance(payload.get("item"), dict):
            out.append(payload["item"])
        for value in payload.values():
            out.extend(_walk_items(value, depth + 1))
    else:
        for item in payload:
            out.extend(_walk_items(item, depth + 1))
    return out


def _iso_or_snowflake(created: Any, item_id: str) -> datetime | None:
    """TikTok's `createTime` is unix seconds; fall back to the id's snowflake."""
    with contextlib.suppress(TypeError, ValueError, OSError, OverflowError):
        if created is not None:
            return datetime.fromtimestamp(float(created), tz=UTC)
    return tiktok_created_at(item_id)


def _mentions(text: str, tag: str | None) -> list[str]:
    """Tickers a caption refers to.

    Hashtags count here and do not on X: `#WIF` is how a ticker is written on
    both these platforms, where a bare `$WIF` is far less common. The tag the
    page was fetched under is included because a post on a hashtag page is about
    that hashtag whether or not the caption repeats it.
    """
    found = {t.upper().lstrip("$") for t in extract_cashtags(text or "")}
    found.update(h.upper().lstrip("#") for h in extract_hashtags(text or ""))
    if tag:
        found.add(tag.upper())
    return sorted(t for t in found if t)


__all__ = [
    "INSTAGRAM_APP_ID",
    "INSTAGRAM_BASE",
    "INSTAGRAM_CAPTURE",
    "INSTAGRAM_LOGIN_PATH",
    "INSTAGRAM_POPULAR_PATH",
    "SEARCH_GATED_ERROR",
    "SIGNED_OUT_ERROR",
    "TIKTOK_BASE",
    "TIKTOK_CAPTURE",
    "TIKTOK_OEMBED",
    "TIKTOK_OEMBED_RPM",
    "InstagramCollector",
    "TikTokCollector",
    "hashtag_for",
    "is_signed_out",
    "tiktok_created_at",
    "tiktok_links",
    "tiktok_video_id",
]
