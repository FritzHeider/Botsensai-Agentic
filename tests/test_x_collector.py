"""X search depth: scroll, dedupe, and what the depth is actually for.

The old browser path took one capture off the search page — roughly twenty posts
— and stopped. Every social-authenticity metric has an evidence floor above
that, so a token being discussed heavily and a token nobody had heard of
produced the same answer: not enough data. These tests pin the three things that
change that, and the one thing that must not change when the browser is absent.

Two layers are exercised deliberately. The collector tests drive a fake driver,
which is fast and covers the parsing; the driver tests drive the *real*
`WebUseDriver` scroll loop against a scripted page, because a fake driver cannot
fail the way the real loop can — it has no buffer to forget to drain, and no
clock to run past. Pinning the formatter is not pinning the producer.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from botsensai.collectors.browser import (
    BrowserUnavailableError,
    WebUseDriver,
)
from botsensai.collectors.social import (
    X_PUBLIC_SEARCH_DEADLINE_SECONDS,
    X_SEARCH_DEADLINE_SECONDS,
    X_SEARCH_POST_TARGET,
    XCollector,
)
from botsensai.config import Settings
from botsensai.models import Chain, TokenRef

FIXTURES = Path(__file__).parent / "fixtures" / "x"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


SEARCH_PAGES = [
    "search_timeline_page1.json",
    "search_timeline_page2.json",
    "search_timeline_page3.json",
]


class FakeDriver:
    """A driver that pays out one recorded page per scroll.

    Mirrors the real contract exactly where it matters: `on_batch` is called
    once per round with only that round's bodies, and a False return stops the
    harvest. Recording `rounds` is what lets a test prove the harvest *stopped*
    rather than merely that it returned the right number of posts.
    """

    def __init__(self, pages: list[Any], *, repeat_last: int = 0) -> None:
        self.pages = list(pages) + [None] * repeat_last
        self.rounds = 0
        self.calls: list[dict[str, Any]] = []

    async def harvest_json(self, url: str, pattern: str, *, on_batch=None, **kwargs: Any) -> list[Any]:
        self.calls.append({"url": url, "pattern": pattern, **kwargs})
        bodies: list[Any] = []
        deadline = float(kwargs.get("deadline_seconds", 0.0))
        idle_allowed = int(kwargs.get("idle_scrolls", 2))
        idle = 0
        for page in self.pages:
            self.rounds += 1
            batch = [] if page is None else [page]
            bodies.extend(batch)
            idle = 0 if batch else idle + 1
            if on_batch is not None and not on_batch(batch):
                break
            if idle >= idle_allowed or deadline <= 0.0:
                break
        return bodies


def harvest(collector: XCollector, driver: Any, query: str = "$CHEEMS", **kwargs: Any) -> list:
    return asyncio.run(collector.harvest_search(driver, query, **kwargs))


# --------------------------------------------------------------------------- #
# depth
# --------------------------------------------------------------------------- #


def test_search_keeps_scrolling_past_the_first_capture():
    """One capture is below every metric's evidence floor; three is not."""
    collector = XCollector(Settings())
    driver = FakeDriver([load(n) for n in SEARCH_PAGES])

    posts = harvest(collector, driver)

    assert driver.rounds == 3, "the harvest must consume every page the feed pays out"
    # 8 + 8 + 8 posted, with page 2 repeating page 1's first three.
    assert len(posts) == 21
    assert len({p.post_id for p in posts}) == len(posts)


def test_repeated_head_of_feed_is_deduped_not_counted_twice():
    """X re-serves the top of the feed on nearly every scroll.

    Counting those again inflates every count-based metric, and concentrates the
    apparent author distribution onto whoever posted the re-served item — a
    shill fingerprint we would have manufactured ourselves.
    """
    collector = XCollector(Settings())
    naive = 0
    for name in SEARCH_PAGES:
        body = load(name)
        naive += len(
            {
                str((n.get("legacy") or n).get("id_str") or n.get("rest_id"))
                for n in XCollector._walk_for_tweets(body)
            }
        )

    posts = harvest(collector, FakeDriver([load(n) for n in SEARCH_PAGES]))

    assert naive == 24, "the fixtures must actually contain the duplicate X serves"
    assert len(posts) == 21, "three re-served posts must not be counted twice"


def test_every_harvested_post_keeps_its_author():
    collector = XCollector(Settings())
    posts = harvest(collector, FakeDriver([load("search_timeline_page1.json")]))

    assert posts, "fixture must produce posts"
    assert all(p.author != "unknown" for p in posts), [p.author for p in posts]
    assert all(p.author_created_at is not None for p in posts)


def test_dedupe_keeps_the_parse_that_recovered_the_author():
    """The same post arrives twice, and one of the two arrivals has no author.

    The capture pattern matches `TweetResultByRestId` as well as
    `SearchTimeline`, and responses are absorbed in network order, so the bare
    `legacy` blob genuinely can land before the full node. Keeping whichever
    came first then files the post under `unknown` with no creation date — not a
    lost post but a fabricated one, and it lands straight in the author
    distribution that `mention_author_diversity` reads as concentration.
    """
    bare = {"id_str": "1700000000000000001", "full_text": "$CHEEMS", "created_at": "Sun Aug 02 14:00:00 +0000 2026"}
    full = {
        "rest_id": "1700000000000000001",
        "legacy": dict(bare),
        "core": {
            "user_results": {
                "result": {
                    "rest_id": "55",
                    "core": {"screen_name": "earlyadopter", "created_at": "Mon Mar 14 12:00:00 +0000 2011"},
                }
            }
        },
    }

    collector = XCollector(Settings())
    posts = harvest(collector, FakeDriver([{"impoverished": bare}, {"rich": full}]))

    assert len(posts) == 1, "both arrivals are the same post"
    assert posts[0].author == "earlyadopter"
    assert posts[0].author_created_at is not None


def test_harvest_stops_at_the_post_target_rather_than_the_page_count():
    collector = XCollector(Settings())
    driver = FakeDriver([load(n) for n in SEARCH_PAGES])

    posts = harvest(collector, driver, limit=10)

    assert len(posts) == 10
    assert driver.rounds == 2, "it must stop asking once the target is met"


def test_the_two_paths_get_different_budgets_on_purpose():
    """A logged-out search page is a login wall, and scrolling a wall is free of
    information and not free of time."""
    public = XCollector(Settings())
    assert public.search_budget() == (X_SEARCH_POST_TARGET, X_PUBLIC_SEARCH_DEADLINE_SECONDS)

    from botsensai.collectors.x_session import AuthenticatedXCollector

    settings = Settings()
    settings.browser.user_data_dir = "/tmp/some-x-profile"
    session = AuthenticatedXCollector(settings)
    assert session.search_budget() == (X_SEARCH_POST_TARGET, X_SEARCH_DEADLINE_SECONDS)
    assert X_SEARCH_DEADLINE_SECONDS == 180.0, "the plan's ceiling is three minutes"

    # And the enrich timeout has to cover the budget it just granted itself,
    # or the base class kills the sweep with nothing stored and nothing logged.
    per_attempt = session.config.timeout_seconds * (session.config.max_retries + 2)
    assert per_attempt > X_SEARCH_DEADLINE_SECONDS * session.session_settings.max_tokens_per_sweep


def test_operator_can_trade_depth_for_sweep_latency():
    collector = XCollector(Settings())
    collector.config.extra["search_post_target"] = 25
    collector.config.extra["search_deadline_seconds"] = 5.0
    assert collector.search_budget() == (25, 5.0)


def test_query_is_percent_encoded():
    """A ticker is operator-supplied text. An unencoded `#` truncates the URL at
    the fragment and searches for something else, returning a plausible page of
    results for the wrong query — worse than an error."""
    collector = XCollector(Settings())
    driver = FakeDriver([load("search_timeline_page1.json")])
    harvest(collector, driver, query="$C#EEMS&f=top")

    url = driver.calls[0]["url"]
    assert "%23" in url and "%26" in url
    assert url.count("&f=") == 1


# --------------------------------------------------------------------------- #
# what the depth is for
# --------------------------------------------------------------------------- #


def test_replies_carry_text_and_author_creation_dates():
    """Reply text and engager ages exist on no other free path, so if they do not
    survive this parse they do not exist at all."""
    collector = XCollector(Settings())
    posts = harvest(collector, FakeDriver([load("tweet_detail_conversation.json")]))

    replies = [p for p in posts if p.parent_id or p.post_id != posts[0].post_id]
    assert len(replies) >= 12, "reply_template_ratio needs twelve before it will speak"
    assert all(r.text.strip() for r in replies)
    aged = [r for r in replies if r.author_created_at is not None]
    assert len(aged) >= 8, "engager_age_dispersion needs eight known creation dates"


def test_a_provisioned_fleet_scores_below_a_real_audience():
    """The end-to-end point of the whole task.

    Page 1's authors were created across thirteen years; page 3's were created in
    one week. If the harvest carries creation dates through, the metric separates
    them; if it drops them, both read MISSING and the depth bought nothing.
    """
    from botsensai.metrics import build_registry
    from tests.test_metrics import context_for

    metric = build_registry().get("engager_age_dispersion")
    assert metric is not None

    collector = XCollector(Settings())
    real = harvest(collector, FakeDriver([load("search_timeline_page1.json")]))
    fleet = harvest(collector, FakeDriver([load("search_timeline_page3.json")]))

    ctx = context_for("organic", seed=3)
    ctx.as_of = datetime(2026, 8, 3, tzinfo=UTC)

    ctx.posts = list(real)
    dispersed = metric.evaluate(ctx)
    ctx.posts = list(fleet)
    clustered = metric.evaluate(ctx)

    assert dispersed.normalized is not None, dispersed.notes
    assert clustered.normalized is not None, clustered.notes
    assert dispersed.normalized > clustered.normalized, (
        f"a decade of accounts must outscore one week: "
        f"{dispersed.normalized:.3f} vs {clustered.normalized:.3f}"
    )


def test_reply_text_reaches_the_template_detector():
    from botsensai.metrics import build_registry
    from tests.test_metrics import context_for

    metric = build_registry().get("reply_template_ratio")
    assert metric is not None

    collector = XCollector(Settings())
    posts = harvest(collector, FakeDriver([load("tweet_detail_conversation.json")]))
    root = posts[0]
    replies = [
        p if p.parent_id else p.model_copy(update={"parent_id": root.post_id})
        for p in posts[1:]
    ]

    ctx = context_for("organic", seed=4)
    ctx.posts = [root, *replies]
    value = metric.evaluate(ctx)

    assert value.raw is not None, value.notes
    # Eight of the twelve replies came off one skeleton.
    assert value.raw >= 0.5, f"scripted replies must cluster, got {value.raw:.3f}"


# --------------------------------------------------------------------------- #
# degradation
# --------------------------------------------------------------------------- #


def test_no_browser_degrades_to_syndication_and_says_so(monkeypatch):
    """Falling back is correct. Doing it quietly is not: a thin social reading
    reported as healthy gets attributed to the token, not to the collector."""
    import botsensai.collectors.social as social

    def no_browser(_settings=None):
        raise BrowserUnavailableError("playwright is not installed")

    monkeypatch.setattr(social, "get_driver", no_browser)

    collector = XCollector(Settings())
    collector.config.extra["handles"] = {}
    token = TokenRef(chain=Chain.SOLANA, mint="1" * 44, symbol="CHEEMS")

    result = asyncio.run(collector.enrich([token]))

    assert result.degraded is True
    assert "syndication" in str(result.raw.get("browser", ""))
    assert collector._browser_failed is True

    # And it must not keep launching a browser it already knows is absent.
    asyncio.run(collector.enrich([token]))
    assert collector._browser_failed is True


def test_a_page_crash_never_raises_out_of_the_collector(monkeypatch):
    """A dead surface degrades the sweep; it does not break it."""
    import botsensai.collectors.social as social

    class Exploding:
        async def harvest_json(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            raise RuntimeError("target page crashed")

    monkeypatch.setattr(social, "get_driver", lambda _s=None: Exploding())

    collector = XCollector(Settings())
    assert asyncio.run(collector.search_via_browser("$CHEEMS")) == []
    # A crash is not the same as an absent browser: the next token still tries.
    assert collector._browser_failed is False


# --------------------------------------------------------------------------- #
# the real driver loop
# --------------------------------------------------------------------------- #


class FakeResponse:
    def __init__(self, url: str, body: Any) -> None:
        self.url = url
        self.status = 200
        self.headers = {"content-type": "application/json"}
        self._body = body
        self.request = type("Req", (), {"method": "GET", "resource_type": "xhr"})()

    async def json(self) -> Any:
        return self._body


class FakeMouse:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def wheel(self, _x: int, _y: int) -> None:
        self.page.scrolls += 1
        await self.page.emit()


class FakePage:
    """A page that pays out one GraphQL response per scroll, like the real feed."""

    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages
        self.scrolls = 0
        self.served = 0
        self.handlers: list[Any] = []
        self.mouse = FakeMouse(self)
        self.closed = False

    def on(self, event: str, handler: Any) -> None:
        if event == "response":
            self.handlers.append(handler)

    async def route(self, _pattern: str, _handler: Any) -> None:
        return None

    async def goto(self, _url: str, **_kwargs: Any) -> Any:
        await self.emit()
        return type("Resp", (), {"status": 200})()

    async def emit(self) -> None:
        if self.served >= len(self.pages):
            return
        body = self.pages[self.served]
        self.served += 1
        if body is None:
            return
        response = FakeResponse("https://x.com/i/api/graphql/abc/SearchTimeline", body)
        for handler in self.handlers:
            handler(response)
        await asyncio.sleep(0)

    async def wait_for_timeout(self, _ms: int) -> None:
        await asyncio.sleep(0)

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def new_page(self) -> FakePage:
        return self.page


def driver_over(page: FakePage) -> WebUseDriver:
    driver = WebUseDriver()
    driver.settings.block_resources = []
    # Installing the context is what keeps `start()` from launching Chromium:
    # it returns early when one is already present.
    driver._context = FakeContext(page)
    return driver


def test_driver_hands_each_round_to_the_caller_as_it_arrives():
    """The reason `capture_json` could not be reused.

    It returns everything only after the last scroll, so the caller cannot say
    "that is enough" — the cost is already paid. This asserts the batches are
    delivered separately, which is the whole mechanism.
    """
    page = FakePage([{"page": i} for i in range(5)])
    driver = driver_over(page)
    seen: list[list[Any]] = []

    async def run() -> list[Any]:
        return await driver.harvest_json(
            "https://x.com/search?q=x",
            "SearchTimeline",
            on_batch=lambda batch: (seen.append(batch), len(seen) < 3)[1],
            wait_ms=0,
            scroll_pause_ms=0,
            deadline_seconds=30.0,
        )

    bodies = asyncio.run(run())

    # One batch per round, each carrying only what that round produced — and the
    # feed's remaining two pages are never fetched, because the caller said stop.
    assert seen == [[{"page": 0}], [{"page": 1}], [{"page": 2}]]
    assert page.scrolls == 2, "it must stop scrolling when told to"
    assert page.served == 3, "the last two pages must never be paid for"
    assert bodies and page.closed


def test_driver_stops_when_the_feed_stops_paying_out():
    """A feed goes quiet when it is exhausted, throttled, or behind a login wall.
    Scrolling at it for the rest of the budget buys nothing and costs the sweep.
    """
    page = FakePage([{"page": 0}, None, None, None, None, None])
    driver = driver_over(page)

    async def run() -> list[Any]:
        return await driver.harvest_json(
            "https://x.com/search?q=x",
            "SearchTimeline",
            wait_ms=0,
            scroll_pause_ms=0,
            deadline_seconds=600.0,
            idle_scrolls=2,
        )

    bodies = asyncio.run(run())

    assert bodies == [{"page": 0}]
    assert page.scrolls == 2, f"two idle rounds should end it, scrolled {page.scrolls}"


def test_driver_returns_the_first_capture_even_with_no_time_to_scroll():
    """Returning empty because the clock was tight is indistinguishable from a
    token nobody is posting about. Those two must never look alike."""
    page = FakePage([{"page": 0}, {"page": 1}])
    driver = driver_over(page)

    bodies = asyncio.run(
        driver.harvest_json(
            "https://x.com/search?q=x",
            "SearchTimeline",
            wait_ms=0,
            scroll_pause_ms=0,
            deadline_seconds=0.0,
        )
    )

    assert bodies == [{"page": 0}]
    assert page.scrolls == 0


def test_driver_survives_a_page_that_dies_mid_scroll():
    """Half a harvest is worth keeping; the responses already captured are real."""

    class Dying(FakePage):
        async def wait_for_timeout(self, ms: int) -> None:
            if self.scrolls >= 1:
                raise RuntimeError("target closed")
            await super().wait_for_timeout(ms)

    page = Dying([{"page": 0}, {"page": 1}, {"page": 2}])
    driver = driver_over(page)

    bodies = asyncio.run(
        driver.harvest_json(
            "https://x.com/search?q=x",
            "SearchTimeline",
            wait_ms=0,
            scroll_pause_ms=1,
            deadline_seconds=30.0,
        )
    )

    assert {"page": 0} in bodies
    assert page.closed, "the page must be closed even when it died"


@pytest.mark.parametrize("bad", [0, -5])
def test_a_zero_or_negative_scroll_budget_still_captures_the_load(bad):
    page = FakePage([{"page": 0}, {"page": 1}])
    driver = driver_over(page)

    bodies = asyncio.run(
        driver.harvest_json(
            "https://x.com/search?q=x",
            "SearchTimeline",
            wait_ms=0,
            scroll_pause_ms=0,
            max_scrolls=bad,
            deadline_seconds=30.0,
        )
    )

    assert bodies == [{"page": 0}]
    assert page.scrolls == 0
