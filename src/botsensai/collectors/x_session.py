"""Session-backed X collection.

This module exists because the three signals with the most weight in the social
family are not available on any free unauthenticated path, at all:

* **Reply text.** The input to `reply_template_ratio`, the highest-weighted
  social metric. The syndication payloads carry reply *counts* and no reply
  bodies, so the template-clustering detector is entirely blind on X without a
  session.
* **Views and bookmarks.** `engagement_depth_ratio` was designed around
  bookmarks specifically because nobody sells them. Both fields are absent from
  every free path — verified by string-searching the full payloads.
* **Per-engager account data.** Who liked and reposted, and when those accounts
  were created. That is what `engager_age_dispersion` reads to catch fleets
  registered in a single batch.

Together those are roughly 18% of the composite weight, currently running near
zero coverage on X.

Operating rules, all enforced in code rather than merely documented:

* **The session is verified, never assumed.** `verify_session()` loads a page
  that renders differently for logged-in and anonymous visitors and checks for
  an authenticated marker. If the profile is not logged in, this collector
  reports `degraded` and says exactly what to fix. It never silently falls
  through to the anonymous path and calls the result "no activity" — that is the
  same silent-absence bug that made the old syndication endpoints so dangerous.
* **Botsensai never handles a password.** There is no login flow here, no
  credential field, and no cookie jar in the repo. The operator logs in once,
  by hand, in a dedicated Chrome profile; this reads that profile.
* **Use a burner.** Sustained automated reads from a logged-in account are
  against X's terms and the realistic consequence is suspension. The default
  pacing below is set to what an actively browsing human plausibly generates,
  which reduces but does not eliminate that risk. The account you point this at
  should be one you are willing to lose.
* **Read-only.** Nothing here posts, follows, likes, replies, DMs or modifies
  any state. It opens public pages and reads the JSON the page fetches for
  itself.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from botsensai.collectors.base import CollectionResult, tag_posts
from botsensai.collectors.browser import BrowserUnavailableError, PageResult, WebUseDriver
from botsensai.collectors.social import XCollector, _int, _iso
from botsensai.config import BrowserSettings, Settings
from botsensai.models import Platform, SocialAccount, SocialPost, TokenRef
from botsensai.util.logging import get_logger

log = get_logger(__name__)

#: GraphQL operation names, matched as URL substrings on captured responses.
OP_SEARCH = r"SearchTimeline"
OP_TWEET_DETAIL = r"TweetDetail"
OP_FAVORITERS = r"Favoriters"
OP_RETWEETERS = r"Retweeters"
OP_USER = r"UserByScreenName"
#: The viewer's own account. Distinct from OP_USER on purpose: that one returns
#: whatever profile the page is displaying, which on a logged-out probe is not
#: the session user at all.
OP_VIEWER = r"Viewer"

CAPTURE_ALL = rf"({OP_SEARCH}|{OP_TWEET_DETAIL}|{OP_FAVORITERS}|{OP_RETWEETERS}|{OP_USER})"

#: Conservative default. GraphQL limits are per-account, per-endpoint, in
#: 15-minute windows; historically a few hundred requests per window. Twenty a
#: minute is well inside that and looks like a person reading actively.
AUTHENTICATED_RPM = 20.0

#: X server-renders a `__META_DATA__` blob into every page carrying an explicit
#: `isLoggedIn` flag. It is the only unambiguous signal on the page: it comes
#: from the server's view of the session cookie rather than from whatever the
#: client-side app happened to render, so it cannot be confused by page content.
#: Everything below it is a fallback for the day X renames this.
META_LOGGED_IN = re.compile(r'"isLoggedIn"\s*:\s*(true|false)')

#: Markers that only appear for a logged-in viewer.
#:
#: Deliberately does NOT include `"is_blue_verified"` or `primaryColumn`. Both
#: appear on ordinary *anonymous* profile pages, and that is not hypothetical:
#: `x.com/home` served to a logged-out visitor renders the public profile of the
#: account whose handle is literally "home", which carries both markers and none
#: of the logged-out ones. That combination reported a dead session as live and
#: named the wrong account, which is precisely the silent-absence failure this
#: module exists to prevent.
LOGGED_IN_MARKERS = (
    'data-testid="SideNav_AccountSwitcher_Button"',
    'data-testid="AppTabBar_Profile_Link"',
    'data-testid="SideNav_NewTweet_Button"',
)

#: Markers that mean the profile is anonymous or the session expired.
LOGGED_OUT_MARKERS = (
    "Sign in to X",
    "Sign up for X",
    'data-testid="loginButton"',
    "/i/flow/login",
)


@dataclass
class SessionStatus:
    """The result of checking whether the browser profile is usable."""

    authenticated: bool
    profile_dir: str | None
    handle: str | None = None
    detail: str = ""

    def explain(self) -> str:
        if self.authenticated:
            who = f" as @{self.handle}" if self.handle else ""
            return f"X session active{who} (profile: {self.profile_dir})"
        if not self.profile_dir:
            return (
                "no browser profile configured — set browser.user_data_dir to a Chrome "
                "profile directory that is already logged in to X"
            )
        return f"X session not authenticated ({self.detail or 'no logged-in marker found'})"


class AuthenticatedXCollector(XCollector):
    """X collection through a logged-in browser profile.

    Subclasses the public collector so all the response parsing is shared, and
    so that when no session is available this degrades to exactly the
    unauthenticated behaviour rather than to nothing.
    """

    name = "x"
    description = "X search, reply text and engager metadata via a logged-in browser profile"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self._session: SessionStatus | None = None
        self._driver: WebUseDriver | None = None
        self.session_settings = self.settings.x_session
        # A session profile only helps if the browser actually uses it, so this
        # collector runs its own driver rather than sharing the global one.
        self.rpm = float(self.session_settings.requests_per_minute or AUTHENTICATED_RPM)
        self.config.requests_per_minute = max(self.config.requests_per_minute, int(self.rpm))
        # The base class kills an op at timeout_seconds * (max_retries + 2). The
        # default 20s allows 100s, and a session enrich cannot finish in that: a
        # single headed search waits 5s then scrolls four times at 1400ms before
        # page load is even counted. Measured on a live sweep: enrich was killed
        # at 100s having stored nothing, while the session verified healthy and
        # no collector error was logged — the same silent-absence failure as the
        # walker depth bug, one layer up.
        self.config.timeout_seconds = max(
            self.config.timeout_seconds, self._enrich_budget_seconds()
        )

    def _enrich_budget_seconds(self) -> float:
        """Per-attempt seconds the base class must allow for a full enrich pass.

        Sized from the work actually configured rather than a round number, so
        raising `max_tokens_per_sweep` cannot silently reintroduce the timeout.

        Because enrich surfaces run concurrently under `asyncio.gather`, this
        budget sets the floor on total sweep duration whenever session
        collection is active — `max_tokens_per_sweep` is the latency dial.
        """
        tokens = max(1, int(self.session_settings.max_tokens_per_sweep))
        # Worst case per token: search, then the thread, then two engager lists.
        loads_per_token = 4
        seconds_per_token = 45.0
        pacing = (tokens * loads_per_token) / max(self.rpm, 1.0) * 60.0
        verify = 15.0
        total = verify + tokens * seconds_per_token + pacing
        return total / max(int(self.config.max_retries) + 2, 1)

    # -- profile and driver ------------------------------------------------- #

    @property
    def profile_dir(self) -> str | None:
        configured = self.settings.browser.user_data_dir
        if not configured:
            return None
        return str(Path(configured).expanduser())

    def _browser_settings(self) -> BrowserSettings:
        """A browser configured for reading a logged-in session.

        Two deliberate departures from the default profile: headed rather than
        headless, because X serves a materially different and more
        challenge-prone experience to headless clients even with a valid
        session; and images unblocked, because `derivative_remix_depth` needs
        the media URLs the timeline lazy-loads.
        """
        base = self.settings.browser
        return base.model_copy(
            update={
                "user_data_dir": self.profile_dir,
                "headless": False,
                "block_resources": ["font", "media"],
                "max_pages": 1,
            }
        )

    async def driver(self) -> WebUseDriver:
        if self._driver is None:
            self._driver = WebUseDriver(self._browser_settings())
        return self._driver

    async def aclose(self) -> None:
        await super().aclose()
        if self._driver is not None:
            with contextlib.suppress(Exception):
                await self._driver.stop()
            self._driver = None

    # -- session verification ----------------------------------------------- #

    async def verify_session(self, force: bool = False) -> SessionStatus:
        """Check whether the configured profile is actually logged in.

        This is the load-bearing method in the module. Assuming a session and
        being wrong produces empty results that look exactly like a token with
        no social activity, which is worse than not collecting at all — so the
        check is explicit, cached, and reported.
        """
        if self._session is not None and not force:
            return self._session

        profile = self.profile_dir
        if not profile:
            self._session = SessionStatus(False, None, detail="no user_data_dir configured")
            return self._session
        if not Path(profile).exists():
            self._session = SessionStatus(
                False, profile, detail=f"profile directory does not exist: {profile}"
            )
            return self._session

        try:
            driver = await self.driver()
            page = await driver.visit(
                "https://x.com/home",
                surface=f"{self.name}:session",
                capture_patterns=[OP_USER, OP_VIEWER],
                wait_ms=4500,
                extract_text=True,
                extract_html=True,
                requests_per_minute=self.rpm,
            )
        except BrowserUnavailableError as exc:
            self._session = SessionStatus(False, profile, detail=f"browser unavailable: {exc}")
            return self._session
        except Exception as exc:
            self._session = SessionStatus(False, profile, detail=f"probe failed: {exc}")
            return self._session

        blob = f"{page.html}\n{page.text}"
        logged_out = [m for m in LOGGED_OUT_MARKERS if m in blob]
        logged_in = [m for m in LOGGED_IN_MARKERS if m in blob]
        meta = META_LOGGED_IN.search(page.html or "")

        if meta:
            # Authoritative when present: this is the server's own answer, so it
            # settles the question outright rather than being weighed against
            # page content that can be misread.
            if meta.group(1) == "true":
                self._session = SessionStatus(
                    True, profile, handle=self._handle_from_page(page), detail="authenticated"
                )
            else:
                self._session = SessionStatus(
                    False, profile, detail="server reports the session is logged out"
                )
        elif logged_out and not logged_in:
            self._session = SessionStatus(
                False, profile, detail=f"login wall present ({logged_out[0]!r})"
            )
        elif logged_in:
            self._session = SessionStatus(
                True, profile, handle=self._handle_from_page(page), detail="authenticated"
            )
        else:
            # Ambiguous. Treat as unauthenticated: a false negative costs one
            # collector, a false positive corrupts every social metric.
            self._session = SessionStatus(
                False, profile, detail="no authentication marker found; treating as logged out"
            )

        log.info("x.session", **{"status": self._session.explain()})
        return self._session

    @staticmethod
    def _handle_from_page(page: PageResult) -> str | None:
        """The handle of the *viewing* account, or None.

        Every source here is tied to the session user specifically. The previous
        implementation ended in a bare `"screen_name":"..."` search across the
        whole page, which matches the first user in any payload — on a logged-out
        `x.com/home` that is the unrelated account @home, so a dead session was
        reported as live under someone else's name.

        Returning None is a correct answer and a safe one: the caller renders the
        session as active without a name. Naming the wrong account is not.
        """
        for body in page.json_matching(OP_VIEWER):
            found = _find_screen_name(body) if isinstance(body, dict) else None
            if found:
                return found

        html = page.html or ""

        # The account switcher and profile link in the sidebar belong to the
        # viewer by construction — they are how the viewer reaches their own
        # profile — so a handle read out of them cannot be another account's.
        for pattern in (
            r'data-testid="AppTabBar_Profile_Link"[^>]*href="/([A-Za-z0-9_]{1,15})"',
            r'href="/([A-Za-z0-9_]{1,15})"[^>]*data-testid="AppTabBar_Profile_Link"',
            r'data-testid="UserAvatar-Container-([A-Za-z0-9_]{1,15})"',
        ):
            match = re.search(pattern, html)
            if match:
                return match.group(1)

        # Last resort: the session user's own id, resolved against the user cache
        # the page ships. Scoped by id, so it cannot drift onto another account.
        session_id = re.search(r'"sessionUserId"\s*:\s*"(\d+)"', html)
        if session_id:
            scoped = re.search(
                rf'"{session_id.group(1)}"\s*:\s*\{{[^{{}}]*?"screen_name"\s*:\s*"([A-Za-z0-9_]{{1,15}})"',
                html,
            )
            if scoped:
                return scoped.group(1)
        return None

    async def health_check(self) -> bool:
        if not self.config.enabled:
            return False
        status = await self.verify_session()
        return status.authenticated

    # -- collection --------------------------------------------------------- #

    async def search(self, query: str, limit: int = 120, scrolls: int = 4) -> list[SocialPost]:
        """Live search for a ticker, with reply text where the page loads it."""
        status = await self.verify_session()
        if not status.authenticated:
            return []

        driver = await self.driver()
        try:
            page = await driver.visit(
                f"https://x.com/search?q={query}&f=live",
                surface=self.name,
                capture_patterns=[CAPTURE_ALL],
                wait_ms=5000,
                scrolls=scrolls,
                scroll_pause_ms=1400,
                extract_text=False,
                requests_per_minute=self.rpm,
            )
        except Exception as exc:
            log.warning("x.authenticated_search_failed", query=query, error=str(exc))
            return []

        seen: set[str] = set()
        posts: list[SocialPost] = []
        for body in page.json_matching(CAPTURE_ALL):
            for node in self._walk_for_tweets(body):
                post = self._parse_graphql_tweet(node)
                if post is None or post.post_id in seen:
                    continue
                seen.add(post.post_id)
                posts.append(post)
                if len(posts) >= limit:
                    return posts
        return posts

    async def conversation(self, post_id: str, author: str = "i") -> list[SocialPost]:
        """Reply text for one post — the thing no free path provides.

        `reply_template_ratio` and `conviction_language_share` both read the
        bodies of replies rather than their count, so this method is the entire
        reason the authenticated path is worth its risk.
        """
        status = await self.verify_session()
        if not status.authenticated:
            return []

        driver = await self.driver()
        try:
            page = await driver.visit(
                f"https://x.com/{author}/status/{post_id}",
                surface=self.name,
                capture_patterns=[OP_TWEET_DETAIL],
                wait_ms=4500,
                scrolls=3,
                scroll_pause_ms=1200,
                extract_text=False,
                requests_per_minute=self.rpm,
            )
        except Exception as exc:
            log.debug("x.conversation_failed", post_id=post_id, error=str(exc))
            return []

        replies: list[SocialPost] = []
        seen: set[str] = set()
        for body in page.json_matching(OP_TWEET_DETAIL):
            for node in self._walk_for_tweets(body):
                post = self._parse_graphql_tweet(node)
                if post is None or post.post_id in seen:
                    continue
                seen.add(post.post_id)
                if post.post_id == post_id:
                    replies.append(post)
                    continue
                # Mark it as a reply even when the GraphQL node omits the
                # in_reply_to field, which it does for nested replies.
                if post.parent_id is None:
                    post = post.model_copy(update={"parent_id": post_id})
                replies.append(post)
        return replies

    async def engagers(self, post_id: str, author: str = "i") -> list[SocialAccount]:
        """Accounts that liked or reposted, with creation dates.

        Creation-date dispersion across engagers is the fleet fingerprint that
        `engager_age_dispersion` measures, and the endpoints that expose it are
        authenticated-only.
        """
        status = await self.verify_session()
        if not status.authenticated:
            return []

        driver = await self.driver()
        accounts: dict[str, SocialAccount] = {}
        for path, pattern in (("likes", OP_FAVORITERS), ("retweets", OP_RETWEETERS)):
            try:
                page = await driver.visit(
                    f"https://x.com/{author}/status/{post_id}/{path}",
                    surface=self.name,
                    capture_patterns=[pattern],
                    wait_ms=4000,
                    scrolls=3,
                    scroll_pause_ms=1200,
                    extract_text=False,
                    requests_per_minute=self.rpm,
                )
            except Exception as exc:
                log.debug("x.engagers_failed", post_id=post_id, path=path, error=str(exc))
                continue

            for body in page.json_matching(pattern):
                for user in _walk_for_users(body):
                    account = self._parse_graphql_user(user)
                    if account is not None:
                        accounts.setdefault(account.handle.lower(), account)
        return list(accounts.values())

    def _parse_graphql_user(self, node: dict[str, Any]) -> SocialAccount | None:
        # Same reshape as the tweet author path: X split the user `legacy` blob
        # into core / relationship_counts / tweet_counts / verification, so
        # reading `legacy` alone yields no handle and drops the account silently.
        # `_user_fields` reads both shapes; see its docstring for why the old
        # behaviour was worse than a plain miss.
        legacy = node.get("legacy") if isinstance(node.get("legacy"), dict) else node
        fields = self._user_fields(node)
        handle = fields["screen_name"]
        if not handle:
            return None
        return SocialAccount(
            platform=Platform.X,
            handle=str(handle),
            account_id=str(node.get("rest_id") or legacy.get("id_str") or "") or None,
            created_at=_iso(fields["created_at"]),
            followers=_int(fields["followers"]),
            following=_int(fields["following"]),
            post_count=_int(fields["post_count"]),
            verified=bool(fields["verified"] or node.get("is_blue_verified")),
            verified_type=node.get("verified_type") or legacy.get("verified_type"),
            bio=fields["description"],
            fast_followers=_int(fields["fast_followers"]),
            normal_followers=_int(fields["normal_followers"]),
        )

    # -- enrichment --------------------------------------------------------- #

    async def enrich(self, tokens: Sequence[TokenRef]) -> CollectionResult:
        """Authenticated enrichment, falling back to the public path cleanly.

        The budget shape here matters: search is one page load per token, and
        conversation plus engagers are two or three more on the *single*
        highest-engagement post found. Pulling replies for every post in a
        search result would be dozens of page loads per token and would get the
        account flagged quickly for no additional signal.
        """
        status = await self.verify_session()
        if not status.authenticated:
            log.info("x.falling_back_to_public", reason=status.explain())
            result = await super().enrich(tokens)
            result.degraded = True
            result.error = status.explain()
            return result

        result = self._empty()
        result.raw["session"] = status.explain()

        for token in list(tokens)[: self.session_settings.max_tokens_per_sweep]:
            symbol = (token.symbol or "").strip()
            if not symbol or len(symbol) < 2:
                continue
            query = f"${symbol}" if symbol.isalnum() else symbol

            posts = await self.search(query)
            if not posts:
                result.degraded = True
                continue
            result.posts.extend(tag_posts(posts, token.key))

            # Deepen only the single post most likely to carry the conversation.
            top = max(posts, key=lambda p: (p.replies or 0, p.engagement))
            if (top.replies or 0) >= self.session_settings.reply_threshold:
                replies = await self.conversation(top.post_id, top.author)
                result.posts.extend(tag_posts(replies, token.key))
                result.raw.setdefault("reply_text_available", []).append(token.key)

            if top.engagement >= self.session_settings.engager_threshold:
                accounts = await self.engagers(top.post_id, top.author)
                result.accounts.extend(accounts)
                fleet = [a for a in accounts if a.fast_follower_share is not None]
                if fleet:
                    result.raw.setdefault("fast_follower_share", {})[token.key] = max(
                        a.fast_follower_share or 0.0 for a in fleet
                    )

        return result


def _find_screen_name(node: Any, depth: int = 0) -> str | None:
    if depth > 8:
        return None
    if isinstance(node, dict):
        value = node.get("screen_name")
        if isinstance(value, str) and value:
            return value
        for child in node.values():
            found = _find_screen_name(child, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_screen_name(item, depth + 1)
            if found:
                return found
    return None


def _walk_for_users(payload: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Find user-shaped objects structurally.

    Same reasoning as the tweet walker: the GraphQL envelope changes shape
    regularly, and matching on the fields a user object has survives renames
    that a hardcoded path does not.
    """
    found: list[dict[str, Any]] = []

    # Same depth trap as _walk_for_tweets, same fix. The Favoriters/Retweeters
    # envelope wraps users in the same instructions/entries/itemContent chain
    # that buried tweets at depth 12-14, so a cap of 10 cannot reach them
    # either. Not measured live (it needs a post with enough engagers to load
    # those lists), so this is the tweet-side measurement applied to an
    # identically-shaped envelope rather than an independently verified number.
    def walk(node: Any, level: int = 0) -> None:
        if level > 16 or len(found) > 600:
            return
        if isinstance(node, dict):
            legacy = node.get("legacy")
            core = node.get("core")
            looks_like_user = (
                # Current shape: handle and creation date live under `core`.
                (isinstance(core, dict) and "screen_name" in core)
                # Older shape, still served by some endpoints.
                or (isinstance(legacy, dict) and "screen_name" in legacy)
                # Flattened payloads.
                or ("screen_name" in node and "created_at" in node)
            )
            if looks_like_user:
                found.append(node)
            for value in node.values():
                walk(value, level + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, level + 1)

    walk(payload, depth)
    return found


__all__ = [
    "AUTHENTICATED_RPM",
    "CAPTURE_ALL",
    "LOGGED_IN_MARKERS",
    "LOGGED_OUT_MARKERS",
    "AuthenticatedXCollector",
    "SessionStatus",
]
