"""Instagram and TikTok: the two surfaces that are mostly shut, and must say so.

`cross_platform_propagation_lag` is the metric these feed, and it is unusual in
one dangerous way: **it scores absence bearishly on purpose**. A token with no
off-platform mentions gets a negative reading, because "still contained" really
is information. That makes a silently blind collector worse here than anywhere
else in the system — it does not lose a signal, it manufactures one.

So most of what is pinned below is failure behaviour. Both platforms were probed
live on 2026-08-04 from a headless anonymous browser and both were largely shut,
in more ways than one guess would cover: Instagram sent `/explore/tags/memecoin/`
to `/accounts/login/` and, in the same run, sent `/explore/tags/wif/` to
`/popular/wif/` — a 200 carrying fifteen kilobytes of encyclopedia prose about
Wi-Fi and no posts at all. TikTok answers `api/challenge/detail/` with 403 and
gates search behind a login by its own admission. The tests assert that every
one of those outcomes arrives as a *named degradation*, never as an empty
success — and the `/popular/` case is why the detector is not simply a check for
the string `login`.

The one path measured to work end to end — `oembed`, 20/20 consecutive 200s — is
pinned against a body captured from the live endpoint, and the snowflake decode
is pinned against a video whose real age is known.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from botsensai.collectors.base import CollectionResult
from botsensai.collectors.browser import BrowserUnavailableError, PageResult
from botsensai.collectors.longtail import (
    INSTAGRAM_LOGIN_PATH,
    INSTAGRAM_POPULAR_PATH,
    TIKTOK_OEMBED_RPM,
    InstagramCollector,
    TikTokCollector,
    hashtag_for,
    is_signed_out,
    tiktok_created_at,
    tiktok_links,
    tiktok_video_id,
)
from botsensai.config import Settings
from botsensai.models import Chain, Platform, SocialPost, TokenRef, utcnow

FIXTURES = Path(__file__).parent / "fixtures" / "longtail"
MINT = "Mint111111111111111111111111111111111111111"
TOKEN = TokenRef(chain=Chain.SOLANA, mint=MINT, symbol="WIF")


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def settings() -> Settings:
    return Settings()


class _FakePage:
    """Only the parts of `PageResult` these collectors read."""

    def __init__(
        self, final_url: str = "", bodies: list[Any] | None = None, text: str = ""
    ) -> None:
        self.final_url = final_url
        self.text = text
        self._bodies = bodies or []

    def json_matching(self, pattern: str) -> list[Any]:
        return list(self._bodies)


class _FakeDriver:
    def __init__(self, page: Any = None, raises: Exception | None = None) -> None:
        self.page = page
        self.raises = raises
        self.visits: list[str] = []

    async def visit(self, url: str, **kwargs: Any) -> Any:
        self.visits.append(url)
        if self.raises is not None:
            raise self.raises
        return self.page


class _FakeOembed:
    """Routes oembed by the `url` param, like the real endpoint does."""

    def __init__(self, bodies: dict[str, Any]) -> None:
        self.bodies = bodies
        self.calls: list[str] = []

    async def get_json(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        target = (params or {}).get("url", "")
        self.calls.append(target)
        value = self.bodies.get(target)
        if isinstance(value, Exception):
            raise value
        return value

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# TikTok: link decoding, which is what makes a link into evidence
# --------------------------------------------------------------------------- #


def test_snowflake_decode_matches_a_video_of_known_age() -> None:
    """The whole link-following path rests on this arithmetic.

    `6718335390845095173` is a real @scout2015 video from July 2019. If the
    shift were wrong by a byte the decoded date would be off by decades, and
    every lag this system computes from a link would be fiction.
    """
    decoded = tiktok_created_at("6718335390845095173")
    assert decoded == datetime(2019, 7, 27, 13, 32, 33, tzinfo=UTC)


def test_snowflake_refuses_ids_too_small_to_be_snowflakes() -> None:
    """A short numeric id shifted right is 1970, not a post from 1970."""
    assert tiktok_created_at("12345") is None
    assert tiktok_created_at("not-a-number") is None
    assert tiktok_created_at("") is None


def test_video_id_extraction_covers_the_forms_people_actually_paste() -> None:
    text = (
        "look at this https://www.tiktok.com/@solana.sam/video/7401122334455667788?is_from=1 "
        "and this one https://m.tiktok.com/@a.b/photo/7401122334455669999 "
        "and a short link https://vm.tiktok.com/t/ZS2abc/ "
        "plus an unrelated https://x.com/someone/status/1234567890"
    )
    links = tiktok_links(text)
    assert len(links) == 3
    assert tiktok_video_id(links[0]) == "7401122334455667788"
    assert tiktok_video_id(links[1]) == "7401122334455669999"
    # A short link carries no id: resolving one costs a redirect, and the caller
    # is entitled to know that before spending it.
    assert tiktok_video_id(links[2]) is None


def test_link_extraction_is_deduped_and_order_preserving() -> None:
    url = "https://www.tiktok.com/@solana.sam/video/7401122334455667788"
    assert tiktok_links(f"{url} {url} {url}") == [url]


# --------------------------------------------------------------------------- #
# TikTok: oembed, the one live path
# --------------------------------------------------------------------------- #


def test_resolve_video_builds_a_dated_attributed_post(settings: Settings) -> None:
    """Against a body captured from the live endpoint, not a hand-written one."""
    collector = TikTokCollector(settings)
    url = "https://www.tiktok.com/@scout2015/video/6718335390845095173"
    collector._oembed = _FakeOembed({url: load("tiktok_oembed.json")})

    post = asyncio.run(collector.resolve_video(url, TOKEN))

    assert post is not None
    assert post.platform is Platform.TIKTOK
    assert post.post_id == "6718335390845095173"
    assert post.author == "scout2015"
    assert post.token_key == TOKEN.key
    # The caption arrives complete, hashtags included — that is the whole reason
    # this endpoint is worth having.
    assert "#petsoftiktok" in post.text
    # Timestamp came from the id, not from the payload: oembed carries none.
    assert post.as_of == datetime(2019, 7, 27, 13, 32, 33, tzinfo=UTC)
    assert post.media_urls and post.media_urls[0].startswith("https://")


def test_resolve_video_refuses_a_success_with_no_caption(settings: Settings) -> None:
    """A 200 carrying nothing is a failure, not a post with an empty caption."""
    collector = TikTokCollector(settings)
    url = "https://www.tiktok.com/@a.b/video/7401122334455667788"
    collector._oembed = _FakeOembed({url: {"version": "1.0", "type": "video"}})
    assert asyncio.run(collector.resolve_video(url, TOKEN)) is None


def test_resolve_video_refuses_a_link_it_cannot_place_in_time(settings: Settings) -> None:
    """A short numeric id is not a snowflake, so no date can be recovered from
    it. The post is refused *before* the request, because a `SocialPost` has no
    valid `as_of` to be built with and a metric made entirely of time
    differences has no use for one that is guessed."""
    collector = TikTokCollector(settings)
    fake = _FakeOembed({})
    collector._oembed = fake

    assert asyncio.run(collector.resolve_video("https://www.tiktok.com/@a.b/video/12345")) is None
    assert fake.calls == [], "a link with no recoverable date should cost no request"


def test_resolve_video_survives_a_transport_failure(settings: Settings) -> None:
    collector = TikTokCollector(settings)
    url = "https://www.tiktok.com/@a.b/video/7401122334455667788"
    collector._oembed = _FakeOembed({url: RuntimeError("connection reset")})
    assert asyncio.run(collector.resolve_video(url, TOKEN)) is None


def test_resolve_links_is_budgeted(settings: Settings) -> None:
    """A post carrying forty links is a spam wall, not forty pieces of evidence."""
    collector = TikTokCollector(settings)
    urls = [f"https://www.tiktok.com/@a.b/video/74011223344556{i:05d}" for i in range(40)]
    collector._oembed = _FakeOembed(dict.fromkeys(urls, {"title": "x", "author_unique_id": "a"}))

    posts = asyncio.run(collector.resolve_links(urls, TOKEN))

    assert len(posts) == collector.max_links_per_token
    assert len(collector._oembed.calls) == collector.max_links_per_token


def test_oembed_rate_limit_is_not_widened_past_the_measurement() -> None:
    """20 consecutive calls held at ~230/min; the encoded limit keeps a margin.

    The constraint in PROMPT.md is that a limit may never exceed the verified
    value. This pins the direction of that inequality so a later 'tune-up' has
    to argue with a number.
    """
    assert TIKTOK_OEMBED_RPM <= 230
    assert Settings().collector("tiktok").requests_per_minute <= TIKTOK_OEMBED_RPM


# --------------------------------------------------------------------------- #
# TikTok: the search page, measured shut
# --------------------------------------------------------------------------- #


def test_search_is_not_attempted_without_a_session_and_says_so(settings: Settings) -> None:
    """Nine seconds per token to rediscover a login wall is nine seconds stolen
    from collection that works. But not looking must still read as degraded."""
    settings.browser.user_data_dir = None
    collector = TikTokCollector(settings)
    assert collector.search_enabled is False

    result = asyncio.run(collector.enrich([TOKEN]))

    assert result.posts == []
    assert result.degraded is True
    assert "login-gated" in (result.error or "")


def test_status_echo_bodies_do_not_count_as_a_payload(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TikTok's search page fetches several endpoints that answer 200 with a
    status code and a log id and nothing else. Measured live: `sug_list` came
    back an empty list while the item list 403'd. Counting those as a payload
    is exactly how a login-gated surface reports itself healthy."""
    settings.collectors["tiktok"].extra["search"] = True
    collector = TikTokCollector(settings)
    page = _FakePage(
        bodies=[
            {"status_code": 0, "status_msg": "", "log_id": "abc"},
            {"sug_list": [], "status_code": 0},
            {"itemList": [], "hasMore": False},
        ]
    )
    driver = _FakeDriver(page=page)
    monkeypatch.setattr("botsensai.collectors.longtail.get_driver", lambda *_: driver)

    result = asyncio.run(collector.enrich([TOKEN]))

    assert driver.visits, "the search page should have been attempted"
    assert result.posts == []
    assert result.degraded is True
    assert "login-gated" in (result.error or "")


def test_search_parses_an_item_list_when_one_arrives(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.collectors["tiktok"].extra["search"] = True
    collector = TikTokCollector(settings)
    driver = _FakeDriver(page=_FakePage(bodies=[load("tiktok_item_list.json")]))
    monkeypatch.setattr("botsensai.collectors.longtail.get_driver", lambda *_: driver)

    result = asyncio.run(collector.enrich([TOKEN]))

    assert result.degraded is False
    assert len(result.posts) == 2
    first = next(p for p in result.posts if p.post_id == "7401122334455667788")
    assert first.author == "solana.sam"
    assert first.views == 94211
    assert first.likes == 5120
    assert first.token_key == TOKEN.key
    assert "WIF" in first.mentioned_tokens
    # The second item carries no createTime, so its date has to come from its id
    # rather than being dropped or defaulted to now.
    second = next(p for p in result.posts if p.post_id == "7401122334455669999")
    assert second.as_of == tiktok_created_at("7401122334455669999")
    assert second.as_of is not None and second.as_of.year >= 2024


def test_tiktok_enrich_never_raises_when_the_browser_is_missing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.collectors["tiktok"].extra["search"] = True
    collector = TikTokCollector(settings)
    driver = _FakeDriver(raises=BrowserUnavailableError("playwright is not installed"))
    monkeypatch.setattr("botsensai.collectors.longtail.get_driver", lambda *_: driver)

    result = asyncio.run(collector.run_enrich([TOKEN]))

    assert isinstance(result, CollectionResult)
    assert result.degraded is True
    assert "browser unavailable" in (result.error or "")


# --------------------------------------------------------------------------- #
# Instagram: the login wall, and the parse for when there is a session
# --------------------------------------------------------------------------- #


def test_instagram_without_a_session_degrades_without_opening_a_page(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured: every anonymous hashtag URL ends on a signed-out page. Paying
    ten seconds of browser time per token to confirm that each sweep is a cost
    with no possible return."""
    settings.browser.user_data_dir = None
    collector = InstagramCollector(settings)
    driver = _FakeDriver(page=_FakePage())
    monkeypatch.setattr("botsensai.collectors.longtail.get_driver", lambda *_: driver)

    result = asyncio.run(collector.enrich([TOKEN]))

    assert driver.visits == [], "no page should be opened without a session"
    assert result.posts == []
    assert result.degraded is True
    assert "signed out" in (result.error or "")


def test_instagram_login_redirect_is_degradation_not_absence(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adversarial case: a session is configured but has expired, so the
    page loads, returns HTTP 200, and lands on the login form. Zero posts and a
    success would be indistinguishable from a hashtag nobody has posted under."""
    settings.browser.user_data_dir = "/tmp/profile"
    collector = InstagramCollector(settings)
    page = _FakePage(final_url=f"https://www.instagram.com{INSTAGRAM_LOGIN_PATH}/")
    driver = _FakeDriver(page=page)
    monkeypatch.setattr("botsensai.collectors.longtail.get_driver", lambda *_: driver)

    result = asyncio.run(collector.enrich([TOKEN]))

    assert driver.visits, "a session was configured, so the page should be tried"
    assert result.posts == []
    assert result.degraded is True
    assert "signed out" in (result.error or "")


def test_login_wall_is_detected_when_the_url_does_not_admit_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case a live run actually hit.

    Instagram performs its sign-in redirect in its own JavaScript after
    hydration, so whether the URL shows `/accounts/login/` depends on a race.
    Measured twice against the same URL: once it redirected, once it reported
    the original path while rendering the login form. A URL-only check reported
    that second visit as "the page fetched nothing", which is true, useless, and
    one step closer to being read as absence.
    """
    settings.browser.user_data_dir = "/tmp/profile"
    collector = InstagramCollector(settings)
    page = _FakePage(
        final_url="https://www.instagram.com/explore/tags/wif/",
        text="See everyday moments from your close friends.\nLog into Instagram\nPassword",
    )
    monkeypatch.setattr("botsensai.collectors.longtail.get_driver", lambda *_: _FakeDriver(page))

    result = asyncio.run(collector.enrich([TOKEN]))

    assert result.posts == []
    assert result.degraded is True
    assert "signed out" in (result.error or "")


def test_the_popular_landing_is_recognised_as_signed_out(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disguise that does not look like a failure.

    Measured live: `/explore/tags/wif/` lands on `/popular/wif/` with HTTP 200
    and fifteen kilobytes of readable prose — about **Wi-Fi**, the wireless
    standard. No login form, no error, nothing that reads as broken, and not one
    post. A detector that only knew about `/accounts/login/` would file this as
    "the page fetched nothing", and any future text fallback would file an
    article on wireless networking as social evidence for the token $WIF.
    """
    settings.browser.user_data_dir = "/tmp/profile"
    collector = InstagramCollector(settings)
    page = _FakePage(
        final_url=f"https://www.instagram.com{INSTAGRAM_POPULAR_PATH}wif/?utm_source=explore_tag",
        text="Log In\nSign Up\nWif\nWi-Fi is a wireless networking technology that lets devices",
    )
    monkeypatch.setattr("botsensai.collectors.longtail.get_driver", lambda *_: _FakeDriver(page))

    result = asyncio.run(collector.enrich([TOKEN]))

    assert result.posts == []
    assert result.degraded is True
    assert "signed out" in (result.error or "")


def test_a_signed_out_instagram_is_learned_once_not_every_sweep(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured in a real sweep: a profile *is* configured but Instagram does
    not recognise it, so the collector paid ten seconds per token to be told so
    — and would pay it again on every sweep for the life of the daemon. One
    sweep's worth of that is the price of finding out; the rest is theft from
    collection that works."""
    settings.browser.user_data_dir = "/tmp/profile"
    collector = InstagramCollector(settings)
    driver = _FakeDriver(page=_FakePage(text="Log into Instagram"))
    monkeypatch.setattr("botsensai.collectors.longtail.get_driver", lambda *_: driver)

    first = asyncio.run(collector.enrich([TOKEN]))
    visits_after_first = len(driver.visits)
    second = asyncio.run(collector.enrich([TOKEN]))

    assert visits_after_first > 0, "the first sweep must actually find out"
    assert len(driver.visits) == visits_after_first, "the second must not re-pay"
    # Still degraded, because the surface is still blind. Latching a cost must
    # not latch away the signal that the metric needs to lower its confidence.
    assert second.degraded is True
    assert "signed out" in (second.error or "")
    assert first.degraded is True


def test_a_gated_tiktok_search_is_learned_once_not_every_sweep(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.collectors["tiktok"].extra["search"] = True
    collector = TikTokCollector(settings)
    driver = _FakeDriver(page=_FakePage(bodies=[{"status_code": 0, "log_id": "x"}]))
    monkeypatch.setattr("botsensai.collectors.longtail.get_driver", lambda *_: driver)

    asyncio.run(collector.enrich([TOKEN]))
    visits_after_first = len(driver.visits)
    second = asyncio.run(collector.enrich([TOKEN]))

    assert visits_after_first > 0
    assert len(driver.visits) == visits_after_first
    assert second.degraded is True
    assert "login-gated" in (second.error or "")


def test_each_signed_out_signal_stands_on_its_own() -> None:
    """Three signals, tested one at a time, because in the field they arrive one
    at a time.

    `_drive_page` suppresses a failed `inner_text`, so the rendered text is
    routinely empty while the URL is informative; and the URL is unreliable
    whenever Instagram's redirect loses its race with `domcontentloaded`, which
    leaves only the text. A test that supplies both signals at once passes with
    either one deleted.
    """
    tag_url = "https://www.instagram.com/explore/tags/wif/"

    # URL only — no text at all, the way a suppressed inner_text leaves it.
    assert is_signed_out(_FakePage(final_url=f"https://www.instagram.com{INSTAGRAM_POPULAR_PATH}wif/"))
    assert is_signed_out(_FakePage(final_url=f"https://www.instagram.com{INSTAGRAM_LOGIN_PATH}/"))

    # Text only — the URL never moved.
    assert is_signed_out(_FakePage(final_url=tag_url, text="Log into Instagram"))
    assert is_signed_out(_FakePage(final_url=tag_url, text="Log In\nSign Up\nWif"))

    # Neither: a hashtag feed that really did load.
    assert not is_signed_out(_FakePage(final_url=tag_url, text="Recent posts\n1,204 posts"))


def test_instagram_reaching_the_page_with_no_readable_json_still_degrades(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.browser.user_data_dir = "/tmp/profile"
    collector = InstagramCollector(settings)
    driver = _FakeDriver(page=_FakePage(final_url="https://www.instagram.com/explore/tags/wif/"))
    monkeypatch.setattr("botsensai.collectors.longtail.get_driver", lambda *_: driver)

    result = asyncio.run(collector.enrich([TOKEN]))

    assert result.degraded is True
    assert "no readable payload" in (result.error or "")


@pytest.mark.parametrize("fixture", ["instagram_web_info.json", "instagram_graphql.json"])
def test_instagram_parses_both_envelopes(settings: Settings, fixture: str) -> None:
    """Instagram serves this data through a REST envelope and a GraphQL one
    whose top-level key carries a version in its name. The walker matches on the
    shape of a post rather than on a path, so both parse with one parser."""
    collector = InstagramCollector(settings)
    posts = collector.parse_bodies([load(fixture)], "wifhat")

    assert len(posts) == 2
    by_id = {p.post_id: p for p in posts}
    lead = next(p for p in posts if "chart" in p.text)
    assert lead.author == "chart.goblin"
    assert lead.likes == 412
    assert lead.as_of == datetime.fromtimestamp(1785820000, tz=UTC)
    assert lead.url and lead.url.endswith("/p/C8xQeLmNoPq/")
    assert "WIFHAT" in lead.mentioned_tokens
    assert "WIF" in lead.mentioned_tokens
    assert all(p.platform is Platform.INSTAGRAM for p in posts)
    assert all(p.author != "unknown" for p in by_id.values())


def test_instagram_dedupes_a_post_seen_on_two_captures(settings: Settings) -> None:
    """A hashtag page refetches the head of the grid on every scroll, exactly as
    X's search timeline does."""
    collector = InstagramCollector(settings)
    body = load("instagram_web_info.json")
    posts = collector.parse_bodies([body, body, body], "wifhat")
    assert len(posts) == 2


def test_instagram_ignores_nodes_that_are_not_posts(settings: Settings) -> None:
    """A caption sub-dict has an id and no timestamp; a user has an id and no
    caption. Neither is a post, and inventing one from either would put a
    fabricated author into the propagation count."""
    collector = InstagramCollector(settings)
    junk = {
        "data": {
            "user": {"id": "51234567", "username": "chart.goblin"},
            "caption": {"pk": "179", "text": "hello"},
            "config": {"id": "x", "taken_at": 1785820000},
        }
    }
    assert collector.parse_bodies([junk], "wifhat") == []


def test_media_nodes_are_not_crowded_out_by_account_rails(settings: Settings) -> None:
    """The adversarial payload: a wall of suggested-account nodes ahead of the
    real post. Each has an id and an owner and would pass a looser filter, and
    there are more of them than the walker's collection cap — so a filter that
    admitted them would return the accounts and lose the only actual post."""
    collector = InstagramCollector(settings)
    rails = [
        {"id": f"u{i}", "pk": f"u{i}", "user": {"id": f"u{i}", "username": f"suggested{i}"}}
        for i in range(600)
    ]
    real = {
        "id": "3401122334455667788",
        "code": "C8xQeLmNoPq",
        "taken_at": 1785820000,
        "caption": {"text": "the only real post here #wifhat"},
        "user": {"id": "51234567", "username": "chart.goblin"},
    }
    posts = collector.parse_bodies([{"rails": rails, "tail": [{"media": real}]}], "wifhat")

    assert len(posts) == 1
    assert posts[0].author == "chart.goblin"


def test_hashtag_refuses_symbols_too_short_to_search(settings: Settings) -> None:
    """A one-character tag returns the whole platform, which is worse than
    returning nothing because it looks like data."""
    assert hashtag_for(TokenRef(chain=Chain.SOLANA, mint=MINT, symbol="$WIF")) == "wif"
    assert hashtag_for(TokenRef(chain=Chain.SOLANA, mint=MINT, symbol="A")) is None
    assert hashtag_for(TokenRef(chain=Chain.SOLANA, mint=MINT, symbol="$")) is None
    assert hashtag_for(TokenRef(chain=Chain.SOLANA, mint=MINT, symbol=None)) is None


# --------------------------------------------------------------------------- #
# doctor: both surfaces must be probe-able with the network dead
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cls", [InstagramCollector, TikTokCollector])
def test_health_check_reports_false_rather_than_raising(
    settings: Settings, cls: type, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`doctor` must exit 0 when both are unreachable. A health check that
    raises is caught by the CLI, but one that hangs is not, and one that returns
    True on a login page is worse than either."""
    collector = cls(settings)

    class _Dead:
        async def get_json(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("network down")

        async def aclose(self) -> None:
            return None

    collector._client = _Dead()
    collector._oembed = _Dead()
    assert asyncio.run(collector.health_check()) is False
    asyncio.run(collector.aclose())


def test_instagram_health_check_rejects_the_html_login_shell(settings: Settings) -> None:
    """Measured: anonymous `api/v1/tags/web_info/` answers HTTP 200 with 605 KB
    of `text/html`. The paced client hands back the text when a body will not
    parse as JSON, so a probe that only checked for truthiness would report this
    dead surface as reachable forever."""
    collector = InstagramCollector(settings)

    class _LoginShell:
        async def get_json(self, *a: Any, **k: Any) -> Any:
            return "<!DOCTYPE html><html>...login...</html>"

        async def aclose(self) -> None:
            return None

    collector._client = _LoginShell()
    assert asyncio.run(collector.health_check()) is False


# --------------------------------------------------------------------------- #
# The pipeline hook: where the metric actually gets its second platform
# --------------------------------------------------------------------------- #


def _post(text: str, token_key: str | None, platform: Platform = Platform.TELEGRAM) -> SocialPost:
    return SocialPost(
        platform=platform,
        post_id=f"p{abs(hash(text)) % 10000}",
        token_key=token_key,
        author="someone",
        as_of=utcnow(),
        observed_at=utcnow(),
        text=text,
        source="test",
    )


def test_pipeline_resolves_tiktok_links_found_on_other_platforms(
    settings: Settings, tmp_path: Any
) -> None:
    """The loop this task exists to close: nobody can search TikTok for a token,
    but people paste TikTok links into the rooms this system already reads."""
    from botsensai.models import Launch
    from botsensai.pipeline import Pipeline

    settings.db_path = str(tmp_path / "probe.db")
    pipeline = Pipeline(settings)
    url = "https://www.tiktok.com/@solana.sam/video/7401122334455667788"
    collector = pipeline.collectors.get("tiktok")
    assert isinstance(collector, TikTokCollector)
    fake = _FakeOembed({url: {"title": "$WIF to zero #wif", "author_unique_id": "solana.sam"}})
    collector._oembed = fake

    launch = Launch(token=TOKEN, created_at=utcnow())
    posts = [
        _post(f"call of the day {url}", TOKEN.key),
        _post("no links here", TOKEN.key),
        # Untagged: it cannot be attributed to a token, so following it would
        # store an unreachable post.
        _post(f"another {url}", None),
    ]

    resolved = asyncio.run(pipeline._follow_offplatform_links(posts, [launch]))
    asyncio.run(pipeline.aclose())

    assert len(resolved) == 1
    assert resolved[0].platform is Platform.TIKTOK
    assert resolved[0].token_key == TOKEN.key
    assert resolved[0].as_of == tiktok_created_at("7401122334455667788")
    assert fake.calls == [url]


def test_pipeline_does_not_follow_tiktoks_own_posts(settings: Settings, tmp_path: Any) -> None:
    """Otherwise every resolved video re-resolves itself next sweep."""
    from botsensai.models import Launch
    from botsensai.pipeline import Pipeline

    settings.db_path = str(tmp_path / "probe.db")
    pipeline = Pipeline(settings)
    collector = pipeline.collectors.get("tiktok")
    assert isinstance(collector, TikTokCollector)
    fake = _FakeOembed({})
    collector._oembed = fake

    url = "https://www.tiktok.com/@solana.sam/video/7401122334455667788"
    posts = [_post(f"see {url}", TOKEN.key, platform=Platform.TIKTOK)]
    launch = Launch(token=TOKEN, created_at=utcnow())

    resolved = asyncio.run(pipeline._follow_offplatform_links(posts, [launch]))
    asyncio.run(pipeline.aclose())

    assert resolved == []
    assert fake.calls == []


def test_pipeline_link_following_is_budgeted(settings: Settings, tmp_path: Any) -> None:
    """Two caps have to hold at once, and the sweep budget must be charged for
    requests actually made rather than for links merely considered — otherwise
    one link-stuffed post exhausts the sweep after six calls."""
    from botsensai.models import Launch
    from botsensai.pipeline import Pipeline

    settings.db_path = str(tmp_path / "probe.db")
    pipeline = Pipeline(settings)
    collector = pipeline.collectors.get("tiktok")
    assert isinstance(collector, TikTokCollector)
    urls = [f"https://www.tiktok.com/@a.b/video/74011223344556{i:05d}" for i in range(50)]
    fake = _FakeOembed(dict.fromkeys(urls, {"title": "x", "author_unique_id": "a"}))
    collector._oembed = fake

    # The adversarial shape: the FIRST token is link-stuffed. Charging the sweep
    # budget for the twenty links it offers rather than the six that will be
    # requested leaves nothing for the tokens behind it, and the second token
    # never gets looked at.
    launches, posts = [], []
    for i, count in enumerate([20, 6, 6, 6]):
        token = TokenRef(chain=Chain.SOLANA, mint=f"{MINT[:-1]}{i}", symbol=f"T{i}")
        launches.append(Launch(token=token, created_at=utcnow()))
        posts.append(_post(" ".join(urls[i * 20 : i * 20 + count]), token.key))

    resolved = asyncio.run(pipeline._follow_offplatform_links(posts, launches))
    asyncio.run(pipeline.aclose())

    assert len(fake.calls) == pipeline.max_link_resolutions_per_sweep
    assert len(resolved) == pipeline.max_link_resolutions_per_sweep
    # Two distinct tokens got service, not one token twice.
    assert len({p.token_key for p in resolved}) == 2


class _RedirectingPage:
    """A page that changes its URL *after* `domcontentloaded`, the way a
    single-page app's own sign-in redirect does."""

    def __init__(self, first: str, after_hydration: str) -> None:
        self.url = first
        self._after = after_hydration
        self.waits = 0

    def on(self, event: str, handler: Any) -> None:
        return None

    async def route(self, _pattern: str, _handler: Any) -> None:
        return None

    async def goto(self, _url: str, **_kwargs: Any) -> Any:
        return type("Resp", (), {"status": 200})()

    async def wait_for_timeout(self, _ms: int) -> None:
        # The redirect lands during the post-navigation wait, which is exactly
        # when a URL read at `domcontentloaded` has already been taken.
        self.waits += 1
        self.url = self._after
        await asyncio.sleep(0)

    async def inner_text(self, _selector: str) -> str:
        return "Log into Instagram"

    async def close(self) -> None:
        return None


def test_visit_reports_the_url_the_page_ended_on_not_the_one_it_started_on() -> None:
    """`final_url` has to mean final.

    Read once at `domcontentloaded`, it is the URL *before* the app has run —
    so every client-side redirect is invisible to it, and a collector checking
    for a sign-in redirect silently stops working. This drives the real
    `WebUseDriver.visit`, not a stand-in, because the bug was in the driver.
    """
    from botsensai.collectors.browser import WebUseDriver

    page = _RedirectingPage(
        "https://www.instagram.com/explore/tags/wif/",
        "https://www.instagram.com/accounts/login/",
    )
    driver = WebUseDriver()
    driver.settings.block_resources = []
    driver._context = type("Ctx", (), {"new_page": lambda self: _async(page)})()

    result = asyncio.run(driver.visit("https://www.instagram.com/explore/tags/wif/", scrolls=0))

    assert page.waits > 0, "the page must have been given time to redirect"
    assert result.final_url == "https://www.instagram.com/accounts/login/"
    assert is_signed_out(result) is True


async def _async(value: Any) -> Any:
    return value


def test_page_result_contract_matches_the_fake() -> None:
    """The fake driver above stands in for `PageResult`. If the real class ever
    loses `final_url` or `json_matching`, these tests would keep passing against
    a shape that no longer exists — the failure mode that let a formatter test
    pass while its producer was broken."""
    page = PageResult(url="https://example.test")
    assert hasattr(page, "final_url")
    assert page.json_matching("anything") == []
