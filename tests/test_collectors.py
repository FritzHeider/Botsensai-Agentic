"""Collector tests.

These target the failure modes that a live integration test would not catch,
because they do not look like failures. The worst one is documented at the top
of `collectors/social.py`: three widely-used X endpoints return HTTP 200 with a
zero-byte body. A collector that trusts the status code records "no social
activity" for every token, forever, and never raises an alarm — which then reads
downstream as a bearish signal about every token in the market.
"""

from __future__ import annotations

import pytest

from botsensai.collectors.social import (
    ARCTIC_SHIFT,
    FOURCHAN_API,
    X_DEAD_ENDPOINTS,
    X_TIMELINE_HOST,
    X_TWEET_HOST,
    EmptySuccessError,
    FourChanBizCollector,
    RedditCollector,
    TelegramChannelCollector,
    XCollector,
    _abbrev_int,
    _require_body,
    _strip_html,
    x_tweet_token,
)
from botsensai.config import load_settings
from botsensai.media.phash import perceptual_values
from botsensai.models import Platform

# --------------------------------------------------------------------------- #
# the silent-absence guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("payload", [None, "", [], {}])
def test_empty_success_is_an_error_not_absence(payload):
    """A 2xx with no body must raise, never be recorded as 'no activity'."""
    with pytest.raises(EmptySuccessError):
        _require_body(payload, "https://example.invalid/thing")


def test_non_empty_body_passes_through():
    assert _require_body({"a": 1}, "u") == {"a": 1}
    assert _require_body("<html>x</html>", "u") == "<html>x</html>"


def test_dead_x_endpoints_are_not_used_anywhere():
    """Regression guard: these return 200 with an empty body and must stay out.

    Verified dead on 2026-07-25. They are the endpoints nearly every published
    scraper still calls, so the temptation to reintroduce one is real.
    """
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[1] / "src" / "botsensai"
    offenders: list[str] = []
    for path in source.rglob("*.py"):
        if path.name == "social.py":
            continue  # defines the deny-list itself
        text = path.read_text(encoding="utf-8")
        for dead in X_DEAD_ENDPOINTS:
            if dead in text:
                offenders.append(f"{path.name}: {dead}")
    assert not offenders, f"dead X endpoints referenced: {offenders}"


def test_x_hosts_are_distinct():
    """The two X hosts have opposite rate limits and must not be conflated."""
    assert X_TIMELINE_HOST != X_TWEET_HOST
    assert "syndication.twitter.com" in X_TIMELINE_HOST
    assert "cdn.syndication.twimg.com" in X_TWEET_HOST


# --------------------------------------------------------------------------- #
# rate limits reflect what was measured
# --------------------------------------------------------------------------- #


def test_x_is_throttled_to_the_measured_limit():
    """Measured: 12 requests per 15 minutes before a hard 429."""
    settings = load_settings()
    config = settings.collector("x")
    assert config.requests_per_minute <= 1, (
        "X profile timeline hard-429s at ~12 requests per 15 minutes"
    )
    assert config.cache_ttl_seconds >= 900, (
        "at that budget, re-fetching a handle within a sweep spends the whole quota"
    )


def test_fourchan_respects_its_documented_one_per_second():
    settings = load_settings()
    assert settings.collector("fourchan").requests_per_minute <= 60


def test_geckoterminal_stays_under_its_shared_budget():
    settings = load_settings()
    assert settings.collector("geckoterminal").requests_per_minute <= 30


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def test_tweet_token_is_deterministic_and_alphanumeric():
    """`tweet-result` rejects requests without this derived token."""
    token = x_tweet_token("1288598630311428102")
    assert token == x_tweet_token("1288598630311428102")
    assert token.isalnum()
    assert "." not in token and "0" not in token
    assert x_tweet_token("not-a-number") == "a"


def test_html_entities_are_decoded_so_cashtags_survive():
    """Both 4chan and Telegram encode `$` as `&#036;`.

    Without decoding, the ticker regex matches nothing and every mention-based
    metric silently reads zero on those two surfaces.
    """
    from botsensai.util.text import extract_cashtags

    decoded = _strip_html("I&#039;m buying &#036;BONK and &#036;WIF <br> now")
    assert decoded == "I'm buying $BONK and $WIF now"
    assert extract_cashtags(decoded) == ["BONK", "WIF"]


def test_telegram_abbreviated_view_counts():
    assert _abbrev_int("3.46M") == 3_460_000
    assert _abbrev_int("1.44K") == 1_440
    assert _abbrev_int("251") == 251
    assert _abbrev_int("2B") == 2_000_000_000
    assert _abbrev_int(None) is None
    assert _abbrev_int("garbage") is None


def test_x_account_never_keys_on_the_always_zero_id():
    """Verified: `user.id` is always 0 in the syndication payload.

    Keying on it collapses every account into one bucket, which would destroy
    `engager_age_dispersion` and every other per-account metric without
    producing a single error.
    """
    collector = XCollector()
    account = collector._parse_account(
        {
            "screen_name": "someone",
            "id": 0,
            "id_str": "1234567890",
            "followers_count": 5000,
            "fast_followers_count": 2000,
            "created_at": "Fri Jul 24 22:40:18 +0000 2026",
        }
    )
    assert account is not None
    assert account.account_id == "1234567890"
    assert account.fast_follower_share == pytest.approx(0.4)


def test_fast_follower_share_needs_both_fields():
    from botsensai.models import SocialAccount

    assert SocialAccount(platform=Platform.X, handle="a").fast_follower_share is None
    assert (
        SocialAccount(platform=Platform.X, handle="a", followers=100).fast_follower_share is None
    )


def test_fourchan_post_carries_native_md5():
    """4chan gives an MD5 per image, so exact cross-surface matching is free.

    It is stored under the `md5:` namespace and not as a bare digest, because
    `derivative_remix_depth` now clusters perceptual hashes in Hamming space: an
    unlabelled MD5 in that column is 128 bits of noise that reads as a distinct
    visual idea, so every plain re-upload of one image would count as a remix.
    """
    collector = FourChanBizCollector()
    post = collector._post_to_social(
        {
            "no": 123,
            "time": 1_753_400_000,
            "com": "buying more &#036;BONK today",
            "tim": 987654321,
            "ext": ".png",
            "md5": "Mx4k4ymW/t8kZXwZbDh8ZQ==",
        },
        thread_no=100,
    )
    assert post is not None
    assert post.platform is Platform.FOURCHAN
    assert post.media_hashes == ["md5:Mx4k4ymW/t8kZXwZbDh8ZQ=="]
    assert perceptual_values(post.media_hashes) == []
    assert post.mentioned_tokens == ["BONK"]
    assert post.parent_id == "100"


def test_reddit_uses_the_mirror_not_the_blocked_host():
    """reddit.com/*.json returns 403 to datacenter IPs; the mirror does not."""
    import inspect

    source = inspect.getsource(RedditCollector.enrich)
    assert "ARCTIC_SHIFT" in source or ARCTIC_SHIFT in source
    assert "arctic-shift" in ARCTIC_SHIFT


def test_collectors_declare_their_capabilities_consistently():
    from botsensai.collectors import ALL_COLLECTORS

    names = [c.name for c in ALL_COLLECTORS]
    assert len(names) == len(set(names)), "duplicate collector name"
    for cls in ALL_COLLECTORS:
        assert cls.can_discover or cls.can_enrich, f"{cls.name} does neither"
        assert cls.description, f"{cls.name} has no description"


def test_fourchan_api_host_is_the_read_only_one():
    assert FOURCHAN_API == "https://a.4cdn.org"


@pytest.mark.asyncio
async def test_collectors_never_raise_out_of_enrich():
    """A dead surface must degrade, not take down the sweep."""
    from botsensai.models import Chain, TokenRef

    token = TokenRef(chain=Chain.SOLANA, mint="1" * 44, symbol="TESTTOKEN")
    for cls in (XCollector, RedditCollector, FourChanBizCollector, TelegramChannelCollector):
        collector = cls()
        collector.config.enabled = False  # forces the disabled path, no network
        try:
            result = await collector.run_enrich([token])
            assert result.degraded is True
            assert result.error == "disabled"
        finally:
            await collector.aclose()


# --------------------------------------------------------------------------- #
# session-backed X collection
# --------------------------------------------------------------------------- #


def test_session_gate_requires_all_three_opt_ins():
    """Turning the feature on and acknowledging the burner are separate acts.

    Built from defaults rather than the on-disk config, because `botsensai
    x-setup` writes a real profile path into that file. Reading it here made the
    test assert something about the operator's machine instead of about the gate.
    """
    from botsensai.config import Settings

    s = Settings()
    assert not s.x_session_enabled, "session collection must be off by default"

    s.x_session.enabled = True
    assert not s.x_session_enabled, "enabling alone must not open the gate"

    s.x_session.acknowledged_burner = True
    assert not s.x_session_enabled, "no profile configured, so still closed"

    s.browser.user_data_dir = "/tmp/some-profile"
    assert s.x_session_enabled


def test_shipped_config_keeps_session_collection_off():
    """The committed config must never ship with the burner opt-ins pre-set.

    Separate from the gate test on purpose: this one is about the file, and the
    only parts of it that must hold on every machine are the two opt-in flags.
    `user_data_dir` is deliberately excluded — it is operator-specific and
    `x-setup` writes it.
    """
    from botsensai.config import load_settings

    s = load_settings()
    assert not s.x_session.enabled
    assert not s.x_session.acknowledged_burner


@pytest.mark.asyncio
async def test_session_verification_refuses_without_a_profile():
    """Must report the problem, never assume a session it does not have."""
    from botsensai.collectors.x_session import AuthenticatedXCollector
    from botsensai.config import Settings

    # Explicit defaults, not the on-disk config: a machine where `x-setup` has
    # run has a real profile configured, and this test is about the no-profile
    # path. Reading the operator's config also made it launch a real browser.
    collector = AuthenticatedXCollector(Settings())
    try:
        status = await collector.verify_session()
        assert status.authenticated is False
        assert status.profile_dir is None
        assert "user_data_dir" in status.explain()
    finally:
        await collector.aclose()


@pytest.mark.asyncio
async def test_session_verification_refuses_a_missing_directory():
    from botsensai.collectors.x_session import AuthenticatedXCollector
    from botsensai.config import load_settings

    settings = load_settings()
    settings.browser.user_data_dir = "/tmp/definitely-not-a-chrome-profile-xyz"
    collector = AuthenticatedXCollector(settings)
    try:
        status = await collector.verify_session()
        assert status.authenticated is False
        assert "does not exist" in status.detail
    finally:
        await collector.aclose()


@pytest.mark.asyncio
async def test_unverified_session_falls_back_and_marks_degraded():
    """The fallback must be visible, not silent.

    Falling back to the public path is correct; doing it quietly is not, because
    the caller would read reduced coverage as a property of the token rather than
    of the collector.
    """
    from botsensai.collectors.x_session import AuthenticatedXCollector
    from botsensai.config import Settings
    from botsensai.models import Chain, TokenRef

    # Explicit defaults, not the on-disk config — same reason as the no-profile
    # test above. Reading the operator's config made this assert the opposite of
    # what it means to: on a machine where the session works, verification
    # succeeds, no fallback happens, and the test fails. It only ever passed
    # because the profile was not logged in.
    collector = AuthenticatedXCollector(Settings())
    # Short-circuit the public browser path too, so the fallback does no network
    # IO; the assertion is about how the fallback is *reported*.
    collector._browser_failed = True
    collector.config.extra["handles"] = {}
    try:
        result = await collector.enrich(
            [TokenRef(chain=Chain.SOLANA, mint="1" * 44, symbol="TESTTOKEN")]
        )
        assert result.degraded is True
        assert result.error, "the fallback must say why it fell back"
    finally:
        await collector.aclose()


def test_ambiguous_page_is_treated_as_logged_out():
    """A false positive corrupts every social metric; a false negative costs one collector."""
    from botsensai.collectors.x_session import LOGGED_IN_MARKERS, LOGGED_OUT_MARKERS

    assert LOGGED_IN_MARKERS and LOGGED_OUT_MARKERS
    assert not set(LOGGED_IN_MARKERS) & set(LOGGED_OUT_MARKERS)


def test_session_collector_registers_under_the_same_surface_name():
    """Exactly one X collector may be active, so both must share the surface name."""
    from botsensai.collectors.social import XCollector
    from botsensai.collectors.x_session import AuthenticatedXCollector

    assert AuthenticatedXCollector.name == XCollector.name == "x"
    assert issubclass(AuthenticatedXCollector, XCollector)


def _search_timeline_envelope(text: str = "gm $CHEEMS", post_id: str = "1234567890") -> dict:
    """A SearchTimeline payload shaped like the one X actually returns.

    The nesting is the point of this fixture, not the contents: measured live,
    tweet nodes sit at depth 12-14 inside this exact chain.
    """
    tweet = {
        "rest_id": post_id,
        "legacy": {
            "id_str": post_id,
            "full_text": text,
            "created_at": "Wed Jul 29 18:00:00 +0000 2026",
            "favorite_count": 3,
            "reply_count": 1,
            "retweet_count": 0,
        },
        "core": {"user_results": {"result": {"legacy": {"screen_name": "someone"}}}},
    }
    return {
        "data": {
            "search_by_raw_query": {
                "search_timeline": {
                    "timeline": {
                        "instructions": [
                            {
                                "type": "TimelineAddEntries",
                                "entries": [
                                    {
                                        "entryId": f"tweet-{post_id}",
                                        "content": {
                                            "entryType": "TimelineTimelineItem",
                                            "itemContent": {
                                                "itemType": "TimelineTweet",
                                                "tweet_results": {"result": tweet},
                                            },
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        }
    }


def test_author_fields_parse_from_the_current_user_shape():
    """Regression: X moved the user fields and we invented a shill signal.

    X removed `legacy` from the user object, splitting it into core /
    relationship_counts / tweet_counts / verification. `rest_id` still resolved,
    so posts were built with author "unknown" rather than being dropped, and
    `mention_author_diversity` then saw ONE distinct author across 390 posts and
    returned raw=1.0 — maximum concentration, a shill fingerprint — at high
    confidence. A parsing miss became fabricated evidence of manipulation, which
    is worse than the MISSING it should have produced.
    """
    from botsensai.collectors.social import XCollector

    node = {
        "rest_id": "555",
        "legacy": {
            "id_str": "555",
            "full_text": "$CHEEMS to the moon",
            "created_at": "Wed Jul 29 18:00:00 +0000 2026",
            "favorite_count": 5,
        },
        "core": {
            "user_results": {
                "result": {
                    "rest_id": "777",
                    # No `legacy` key at all — this is the current shape.
                    "core": {
                        "screen_name": "MEMEINSIGHTS1",
                        "name": "1000X MEME INSIGHTS",
                        "created_at": "Fri May 21 07:55:09 +0000 2010",
                    },
                    "relationship_counts": {"followers": 1172, "following": 1},
                    "tweet_counts": {"tweets": 145271},
                    "verification": {"verified": False},
                }
            }
        },
    }

    post = XCollector()._parse_graphql_tweet(node)
    assert post is not None
    assert post.author == "MEMEINSIGHTS1", "handle must come from core.screen_name"
    assert post.author != "unknown"
    assert post.author_followers == 1172, "followers must come from relationship_counts"
    assert post.author_created_at is not None, "engager_age_dispersion needs this"
    assert post.author_created_at.year == 2010


def test_author_fields_still_parse_from_the_legacy_user_shape():
    """Other endpoints still serve the old shape; both must work."""
    from botsensai.collectors.social import XCollector

    node = {
        "rest_id": "556",
        "legacy": {
            "id_str": "556",
            "full_text": "$LOST",
            "created_at": "Wed Jul 29 18:00:00 +0000 2026",
        },
        "core": {
            "user_results": {
                "result": {
                    "rest_id": "778",
                    "legacy": {
                        "screen_name": "oldshape",
                        "created_at": "Mon Jan 05 10:00:00 +0000 2015",
                        "followers_count": 42,
                    },
                }
            }
        },
    }
    post = XCollector()._parse_graphql_tweet(node)
    assert post is not None
    assert post.author == "oldshape"
    assert post.author_followers == 42
    assert post.author_created_at is not None
    assert post.author_created_at.year == 2015


def test_unresolvable_author_stays_distinct_per_account():
    """Two authorless posts must not collapse into one apparent author.

    A shared placeholder is what turned a parse miss into a manufactured
    single-account signal, so the fallback is the account id.
    """
    from botsensai.collectors.social import XCollector

    def node(post_id: str, user_id: str) -> dict:
        return {
            "rest_id": post_id,
            "legacy": {
                "id_str": post_id,
                "full_text": "hi",
                "created_at": "Wed Jul 29 18:00:00 +0000 2026",
            },
            "core": {"user_results": {"result": {"rest_id": user_id}}},
        }

    collector = XCollector()
    a = collector._parse_graphql_tweet(node("1", "aaa"))
    b = collector._parse_graphql_tweet(node("2", "bbb"))
    assert a is not None and b is not None
    assert a.author != b.author, "distinct accounts must stay distinct"


def test_collected_posts_reach_the_metric_that_needs_them(tmp_path):
    """Regression: posts were stored with token_key NULL and were unreadable.

    `pipeline.enrich` called `insert_posts([post])` without a token, and
    `posts_as_of` filters on that column, so 494 posts accumulated in the table
    while every social metric read MISSING. Complete data, zero reachability,
    no error anywhere — the same silent-absence class as the walker depth and the
    enrich timeout.

    This asserts the whole path a real post travels: collector tags it, the store
    writes it, and a per-token read finds it again.
    """
    from botsensai.collectors.base import tag_posts
    from botsensai.models import Chain, Platform, SocialPost, TokenRef, utcnow
    from botsensai.store.db import Database

    token = TokenRef(chain=Chain.SOLANA, mint="9" * 44, symbol="ROUNDTRIP")
    other = TokenRef(chain=Chain.SOLANA, mint="8" * 44, symbol="OTHER")
    post = SocialPost(
        platform=Platform.X,
        post_id="99887766",
        author="someone",
        as_of=utcnow(),
        text="$ROUNDTRIP looks interesting",
        replies=4,
        views=1000,
        bookmarks=7,
        source="x:graphql",
    )
    assert post.token_key is None, "a freshly parsed post does not know its token"

    tagged = tag_posts([post], token.key)
    assert tagged[0].token_key == token.key

    store = Database(str(tmp_path / "rt.db"))
    try:
        assert store.insert_posts(tagged) == 1

        found = store.posts_as_of(token.key, utcnow())
        assert found, "a tagged post must be readable by the token it belongs to"
        assert found[0].post_id == "99887766"
        assert found[0].token_key == token.key
        assert found[0].bookmarks == 7, "session-only fields must survive the round trip"

        # And it must not leak into an unrelated token's context.
        assert not store.posts_as_of(other.key, utcnow())
    finally:
        store.close()


def test_tag_posts_does_not_overwrite_a_precise_attribution():
    """A coarse caller must not steal a post another collector already attributed."""
    from botsensai.collectors.base import tag_posts
    from botsensai.models import Platform, SocialPost, utcnow

    post = SocialPost(
        platform=Platform.X,
        post_id="1",
        author="a",
        as_of=utcnow(),
        token_key="solana:already-attributed",
    )
    tag_posts([post], "solana:coarse-guess")
    assert post.token_key == "solana:already-attributed"


def test_session_enrich_budget_survives_a_headed_browser_pass():
    """Regression: the default 20s timeout killed session enrich at 100s.

    `run_enrich` aborts at `timeout_seconds * (max_retries + 2)`. One headed
    search waits 5s and scrolls four times at 1400ms before page load counts, so
    a single token costs tens of seconds and six cannot finish in 100s. Measured
    live: enrich was killed at 100s having stored nothing, with a healthy session
    and no error logged — indistinguishable downstream from "nobody is talking
    about these tokens".

    Asserts the budget is derived from the configured work, so raising
    max_tokens_per_sweep cannot silently reintroduce the timeout.
    """
    from botsensai.collectors.x_session import AuthenticatedXCollector
    from botsensai.config import Settings

    settings = Settings()
    settings.browser.user_data_dir = "/tmp/some-profile"
    settings.x_session.max_tokens_per_sweep = 6
    collector = AuthenticatedXCollector(settings)

    effective = collector.config.timeout_seconds * (collector.config.max_retries + 2)
    # Four page loads per token at ~10s of scripted waits each, before latency.
    floor = 6 * 4 * 10.0
    assert effective > floor, f"budget {effective:.0f}s cannot cover six tokens"

    # And it must scale with the work, not be a hardcoded constant.
    settings_more = Settings()
    settings_more.browser.user_data_dir = "/tmp/some-profile"
    settings_more.x_session.max_tokens_per_sweep = 24
    bigger = AuthenticatedXCollector(settings_more)
    assert bigger.config.timeout_seconds > collector.config.timeout_seconds


def test_tweet_walker_reaches_the_real_search_envelope_depth():
    """Regression: a depth cap of 10 silently returned zero tweets.

    The walker gave up two levels short of where SearchTimeline actually puts
    tweets, so every session-gated social metric read MISSING while the session
    verified as healthy and no error was logged anywhere — the exact
    silent-absence failure the module is built to prevent. Measured live: 22
    tweet nodes at depths 12-14, of which a cap of 10 found none.
    """
    from botsensai.collectors.social import XCollector

    payload = _search_timeline_envelope()
    found = XCollector._walk_for_tweets(payload)
    assert found, "walker must reach tweets nested at the real envelope depth"

    collector = XCollector()
    parsed = [p for p in (collector._parse_graphql_tweet(n) for n in found) if p]
    assert parsed, "nodes the walker returns must survive parsing"
    assert any(p.text == "gm $CHEEMS" for p in parsed)
    assert any(p.post_id == "1234567890" for p in parsed)


def test_pipeline_selects_the_collector_by_the_gate():
    from botsensai.collectors.social import XCollector
    from botsensai.collectors.x_session import AuthenticatedXCollector
    from botsensai.config import load_settings
    from botsensai.pipeline import Pipeline

    public = Pipeline(load_settings())
    assert type(public.collectors.get("x")) is XCollector

    settings = load_settings()
    settings.x_session.enabled = True
    settings.x_session.acknowledged_burner = True
    settings.browser.user_data_dir = "/tmp/some-profile"
    session = Pipeline(settings)
    assert isinstance(session.collectors.get("x"), AuthenticatedXCollector)


def test_no_credential_field_exists_anywhere():
    """Structural guard: Botsensai must never be able to hold an X password."""
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[1] / "src" / "botsensai"
    banned = ("x_password", "twitter_password", "def login(", "fill_login", "type_password")
    offenders: list[str] = []
    for path in source.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in banned:
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, f"credential handling found: {offenders}"


def test_purchased_follower_metric_needs_session_data():
    """It must report MISSING without a session, never a confident zero."""
    from botsensai.metrics import build_registry
    from botsensai.models import Confidence

    metric = build_registry().get("purchased_follower_signal")
    assert metric is not None

    from tests.test_metrics import context_for

    ctx = context_for("organic", seed=5)
    value = metric.evaluate(ctx)
    assert value.confidence is Confidence.MISSING
    assert value.normalized is None
    assert "authenticated" in (value.notes or "")

    ctx.extra["fast_follower_share"] = {ctx.token.key: 0.62}
    with_data = metric.evaluate(ctx)
    assert with_data.raw == pytest.approx(0.62)
    # Bearish direction: a high fast-follower share must score low.
    assert (with_data.normalized or 1.0) < 0.5


# --------------------------------------------------------------------------- #
# x-setup
# --------------------------------------------------------------------------- #


def test_setup_refuses_a_profile_inside_a_git_repo(tmp_path):
    """A profile holds live session cookies. Inside a repo it is one `git add -A`
    away from being published, so this is refused rather than gitignored."""
    from botsensai.setup_x import prepare_profile

    (tmp_path / ".git").mkdir()
    created, message = prepare_profile(tmp_path / "nested" / "profile")
    assert created is False
    assert "refusing" in message
    assert not (tmp_path / "nested" / "profile").exists()


def test_setup_creates_a_profile_outside_a_repo(tmp_path):
    from botsensai.setup_x import prepare_profile

    target = tmp_path / "x-profile"
    created, message = prepare_profile(target)
    assert created is True
    assert target.exists()
    assert str(target) in message

    again, message = prepare_profile(target)
    assert again is False
    assert "existing" in message


def test_locked_profile_is_detected(tmp_path):
    """Chrome left running is the most common setup failure and produces an
    unhelpful error deep inside Playwright, so it is caught up front."""
    from botsensai.setup_x import profile_is_locked

    profile = tmp_path / "p"
    profile.mkdir()
    assert profile_is_locked(profile) is False
    (profile / "SingletonLock").touch()
    assert profile_is_locked(profile) is True


def test_config_patch_preserves_comments(tmp_path):
    """The config's comments carry the operational warnings.

    Round-tripping through PyYAML would delete every one of them, which is why
    this is a targeted textual patch.
    """
    from botsensai.setup_x import patch_config

    config = tmp_path / "botsensai.yaml"
    config.write_text(
        "# top comment\n"
        "trading_mode: paper\n"
        "\n"
        "browser:\n"
        "  headless: true\n"
        "  # keep this outside the repo\n"
        "  user_data_dir: null\n"
        "\n"
        "x_session:\n"
        "  enabled: false\n"
        "  acknowledged_burner: false\n"
        "  requests_per_minute: 20\n",
        encoding="utf-8",
    )

    ok, detail = patch_config(config, tmp_path / "prof", acknowledged_burner=True)
    assert ok, detail
    written = config.read_text(encoding="utf-8")

    assert "# top comment" in written
    assert "# keep this outside the repo" in written
    assert f'user_data_dir: "{tmp_path / "prof"}"' in written
    assert "enabled: true" in written
    assert "acknowledged_burner: true" in written
    # Untouched keys survive.
    assert "requests_per_minute: 20" in written
    assert "trading_mode: paper" in written
    # And the original is recoverable.
    assert (tmp_path / "botsensai.yaml.bak").exists()


def test_config_patch_scopes_keys_to_their_section(tmp_path):
    """`enabled` appears in several blocks; the wrong one must not be rewritten."""
    from botsensai.setup_x import patch_config

    config = tmp_path / "c.yaml"
    config.write_text(
        "media:\n  enabled: false\n\nx_session:\n  enabled: false\n  acknowledged_burner: false\n"
        "\nbrowser:\n  user_data_dir: null\n",
        encoding="utf-8",
    )
    ok, _ = patch_config(config, tmp_path / "p", acknowledged_burner=False)
    assert ok
    written = config.read_text(encoding="utf-8")
    media_block = written.split("media:")[1].split("\n\n")[0]
    assert "enabled: false" in media_block, "media.enabled must not have been touched"
    x_block = written.split("x_session:")[1].split("\n\n")[0]
    assert "enabled: true" in x_block


def test_burner_confirmation_is_not_a_yes_no():
    """Typing a specific word makes the acknowledgement a decision, not a default."""
    from botsensai.setup_x import BURNER_CONFIRMATION

    assert BURNER_CONFIRMATION not in ("y", "yes", "", "true")
    assert len(BURNER_CONFIRMATION) >= 4


def test_setup_never_handles_a_credential():
    """Structural guard on the *code*, not the prose.

    The module's docstring says it never touches a credential, which is exactly
    the kind of claim that rots. This walks the AST — so comments and docstrings
    are excluded — and asserts no identifier, attribute or keyword argument
    anywhere in the module names one.
    """
    import ast
    import inspect

    from botsensai import setup_x

    tree = ast.parse(inspect.getsource(setup_x))
    banned = ("password", "passwd", "secret", "credential", "auth_token", "cookie")

    offenders: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            names.append(node.arg)
        elif isinstance(node, ast.FunctionDef):
            names.append(node.name)
            names.extend(a.arg for a in node.args.args)
        for name in names:
            lowered = name.lower()
            if any(b in lowered for b in banned):
                offenders.append(name)

    assert not offenders, f"credential-shaped identifiers in setup_x: {sorted(set(offenders))}"
