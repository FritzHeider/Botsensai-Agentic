"""Configuration for Botsensai.

Every knob lives here and is overridable by environment variable or by
`config/botsensai.yaml`. Nothing anywhere else in the codebase should read
`os.environ` directly.

Safety posture: live trading is off by default and requires TWO independent
opt-ins (`BOTSENSAI_TRADING_MODE=live` and `BOTSENSAI_I_UNDERSTAND_THE_RISK=1`).
Private keys are never read from the repo or from config files, only from an
environment variable or an external keyfile path.
"""

from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "botsensai.yaml"


class TradingMode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class CollectorSettings(BaseModel):
    """Per-surface collector toggles and pacing.

    `web_use_first` reflects the design choice that collection is browser-driven
    by default, with HTTP fast paths used only where a surface publishes a
    stable, documented endpoint.
    """

    enabled: bool = True
    web_use_first: bool = True
    http_fast_path: bool = True
    poll_seconds: float = 15.0
    max_concurrency: int = 4
    requests_per_minute: int = 60
    timeout_seconds: float = 20.0
    max_retries: int = 3
    cache_ttl_seconds: float = 5.0
    base_url: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class BrowserSettings(BaseModel):
    """Playwright / web-use driver settings."""

    engine: str = "chromium"
    headless: bool = True
    executable_path: str | None = None
    user_data_dir: str | None = Field(
        default=None,
        description="Persistent profile dir. Point this at a logged-in profile to reach "
        "surfaces that require a session, e.g. X and Instagram.",
    )
    locale: str = "en-US"
    timezone_id: str = "America/New_York"
    viewport_width: int = 1440
    viewport_height: int = 900
    user_agent: str | None = None
    nav_timeout_ms: int = 30000
    block_resources: list[str] = Field(
        default_factory=lambda: ["image", "media", "font"],
        description="Resource types to abort, for speed. Clear this when scraping media.",
    )
    max_pages: int = 4
    stealth: bool = True
    slow_mo_ms: int = 0
    respect_robots: bool = True


class XSessionSettings(BaseModel):
    """Opt-in for session-backed X collection.

    Reply text, view and bookmark counts, and per-engager account ages are not
    available on any free unauthenticated path, and they are the inputs to most
    of the social-authenticity family. Reading them needs a logged-in browser
    profile.

    Botsensai never handles a credential: there is no password field here, no
    login flow anywhere in the codebase, and no cookie is written into the repo.
    The operator logs in once by hand in a dedicated Chrome profile and points
    `browser.user_data_dir` at it.

    Use an account you are willing to lose. Sustained automated reads from a
    logged-in account are against X's terms and the realistic consequence is
    suspension of that account.
    """

    enabled: bool = False
    #: Set true only after confirming the profile belongs to a throwaway account.
    acknowledged_burner: bool = False
    requests_per_minute: float = 20.0
    max_tokens_per_sweep: int = 6
    #: Minimum replies on a post before spending a page load on its thread.
    reply_threshold: int = 5
    #: Minimum engagement before spending page loads on the engager lists.
    engager_threshold: int = 25
    verify_every_sweep: bool = False


class RiskSettings(BaseModel):
    """Hard limits. These are enforced in the broker, not merely advisory."""

    max_position_native: float = 0.25
    max_portfolio_exposure_native: float = 2.0
    max_concurrent_positions: int = 8
    max_daily_loss_native: float = 1.0
    max_trades_per_hour: int = 20
    min_liquidity_usd: float = 5_000.0
    min_token_age_seconds: float = 45.0
    max_token_age_seconds: float = 60 * 60 * 6
    max_slippage_bps: int = 800
    stop_loss_pct: float = 0.45
    take_profit_multiples: list[float] = Field(default_factory=lambda: [2.0, 4.0, 10.0])
    take_profit_fractions: list[float] = Field(default_factory=lambda: [0.4, 0.3, 0.3])
    trailing_stop_pct: float = 0.35
    max_hold_seconds: float = 60 * 60 * 4
    kill_switch: bool = False

    @model_validator(mode="after")
    def _check_ladder(self) -> RiskSettings:
        if len(self.take_profit_multiples) != len(self.take_profit_fractions):
            raise ValueError("take_profit_multiples and take_profit_fractions must be same length")
        if sum(self.take_profit_fractions) > 1.0 + 1e-9:
            raise ValueError("take_profit_fractions must sum to <= 1.0")
        return self


class ExecutionSettings(BaseModel):
    """Fill modeling. Used identically by the paper broker and the backtester so
    that a paper result and a backtest result are directly comparable."""

    base_latency_ms: float = 450.0
    latency_jitter_ms: float = 250.0
    priority_fee_lamports: int = 1_000_000
    jito_tip_lamports: int = 1_000_000
    platform_fee_bps: int = 100
    lp_fee_bps: int = 30
    fail_probability: float = 0.06
    sandwich_probability: float = 0.10
    sandwich_extra_bps: float = 150.0
    price_impact_model: str = Field(
        default="curve", description="curve | constant_product | depth_table"
    )
    max_impact_bps: float = 5_000.0


class ScoringSettings(BaseModel):
    weights_version: str = "v0"
    weights_path: str | None = "config/weights.json"
    min_coverage: float = 0.5
    entry_threshold: float = 0.68
    exit_threshold: float = 0.35
    regime_lookback_hours: float = 6.0
    # Graduation-rate thresholds separating hot / normal / dead markets.
    #
    # These MUST be revisited periodically. The base rate is strongly
    # non-stationary: pump.fun graduation ran under 2% in Q4 2024 and had fallen
    # to roughly 0.63% by late 2025. Thresholds calibrated to the old regime
    # classify every subsequent day as "dead" and quietly disable the strategy,
    # so they are config rather than constants and the anchor date is recorded.
    regime_hot_graduation_rate: float = 0.010
    regime_dead_graduation_rate: float = 0.0035
    regime_calibrated_on: str = "2025-10 empirical base rate ~0.63%"
    veto_top10_share: float = 0.55
    veto_insider_share: float = 0.30
    veto_bundle_share: float = 0.35
    veto_deployer_rug_count: int = 1
    veto_inauthenticity: float = 0.75


class MemorySettings(BaseModel):
    enabled: bool = True
    path: str = "data/memory.db"
    max_entries_in_prompt: int = 24
    decay_half_life_hours: float = 72.0
    min_confidence_to_apply: float = 0.35
    autowrite_postmortems: bool = True


class MediaSettings(BaseModel):
    enabled: bool = True
    output_dir: str = "data/content"
    disclosure_text: str = (
        "Not financial advice. Automated analysis of publicly available data. "
        "The operator of this system may hold positions in tokens discussed."
    )
    require_disclosure: bool = True
    max_posts_per_hour: int = 6
    blog_template: str = "blog_post.md.j2"
    thread_template: str = "x_thread.md.j2"


class MediaHashSettings(BaseModel):
    """Budget for turning posted images into perceptual hashes.

    Media is the only kilobyte-to-megabyte traffic in a sweep and the decoder is
    pure Python, so every field here is a ceiling rather than a preference. The
    defaults are sized so that a full sweep spends at most a few seconds and a
    few tens of megabytes on imagery, which is the point at which the signal
    stops being worth its displacement of on-chain calls.
    """

    enabled: bool = True
    requests_per_minute: float = 120.0
    max_concurrency: int = 4
    timeout_seconds: float = 8.0
    #: Enforced while streaming, not from `content-length`.
    max_bytes: int = 2_000_000
    #: Refuse to decode beyond this; cost is linear in pixels.
    max_pixels: int = 4_000_000
    max_images_per_post: int = 4
    max_images_per_sweep: int = 60
    deadline_seconds: float = 30.0
    cache_entries: int = 4096
    #: Hash the host's small variant. Perceptual hashes are scale-invariant, and
    #: an X original measured 6590x4690 on 2026-08-04 — past `max_pixels` and no
    #: more informative than the 688px variant it also publishes.
    prefer_thumbnails: bool = True


class BacktestSettings(BaseModel):
    start: str | None = None
    end: str | None = None
    train_days: float = 14.0
    test_days: float = 7.0
    step_days: float = 7.0
    embargo_hours: float = 24.0
    initial_capital_native: float = 10.0
    min_trades_for_significance: int = 50
    bootstrap_samples: int = 2000
    seed: int = 1337


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BOTSENSAI_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    # --- core ---------------------------------------------------------------
    trading_mode: TradingMode = TradingMode.PAPER
    i_understand_the_risk: bool = False
    data_dir: str = "data"
    db_path: str = "data/botsensai.db"
    log_level: str = "INFO"
    log_json: bool = False
    dry_run: bool = True
    seed: int = 1337

    # --- chains and rpc -----------------------------------------------------
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_ws_url: str | None = None
    helius_api_key: str | None = None
    birdeye_api_key: str | None = None
    bitquery_api_key: str | None = None
    jito_block_engine_url: str | None = None

    # --- social credentials (all optional; collectors degrade without them) --
    x_bearer_token: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "botsensai/0.1 (research)"
    telegram_api_id: str | None = None
    telegram_api_hash: str | None = None
    youtube_api_key: str | None = None

    # --- keys (live only) ---------------------------------------------------
    wallet_keyfile: str | None = Field(
        default=None,
        description="Path to a keypair file OUTSIDE the repo. Never commit a key.",
    )

    # --- nested sections ----------------------------------------------------
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    x_session: XSessionSettings = Field(default_factory=XSessionSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    media_hash: MediaHashSettings = Field(default_factory=MediaHashSettings)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)
    collectors: dict[str, CollectorSettings] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _defaults_and_guards(self) -> Settings:
        known = [
            "pumpfun",
            "moonshot",
            "apestore",
            "believe",
            "dexscreener",
            "geckoterminal",
            "gmgn",
            "x",
            "instagram",
            "tiktok",
            "reddit",
            "telegram",
            "youtube",
            "fourchan",
            "solana_rpc",
            "rugcheck",
            "pumpfun_chat",
        ]
        for name in known:
            self.collectors.setdefault(name, CollectorSettings())

        # Public-endpoint pacing defaults that are known to be conservative.
        self.collectors["dexscreener"].requests_per_minute = min(
            self.collectors["dexscreener"].requests_per_minute, 55
        )
        self.collectors["geckoterminal"].requests_per_minute = min(
            self.collectors["geckoterminal"].requests_per_minute, 28
        )
        # Measured live, not guessed. syndication.twitter.com hard-429s after
        # ~12 requests per 15 minutes and stays blocked for ~10 minutes, so the
        # X profile path gets well under one request per minute. 4chan's
        # documented rule is one request per second.
        self.collectors["x"].requests_per_minute = min(
            self.collectors["x"].requests_per_minute, 1
        )
        self.collectors["x"].cache_ttl_seconds = max(
            self.collectors["x"].cache_ttl_seconds, 900.0
        )
        self.collectors["fourchan"].requests_per_minute = min(
            self.collectors["fourchan"].requests_per_minute, 55
        )
        self.collectors["telegram"].requests_per_minute = min(
            self.collectors["telegram"].requests_per_minute, 30
        )
        # Measured 2026-08-04. TikTok's oembed endpoint took 20 consecutive
        # requests in 5.2s (~230/min) with no throttling; 60 keeps a fivefold
        # margin on a burst test. Instagram has no throughput worth budgeting —
        # anonymous access is a login wall, not a rate limit — so it gets the
        # smallest number that still lets a session-backed profile work.
        self.collectors["tiktok"].requests_per_minute = min(
            self.collectors["tiktok"].requests_per_minute, 60
        )
        self.collectors["instagram"].requests_per_minute = min(
            self.collectors["instagram"].requests_per_minute, 6
        )
        # api.mainnet-beta.solana.com is documented at roughly 10 req/s and 429s
        # well before that under load (docs/DATA_SOURCES.md). Funding resolution
        # is the only caller and it is a background nicety, so it gets 2/s — far
        # inside the published limit, and it never queues ahead of a sweep.
        self.collectors["solana_rpc"].requests_per_minute = min(
            self.collectors["solana_rpc"].requests_per_minute, 120
        )

        if self.trading_mode is TradingMode.LIVE and not self.i_understand_the_risk:
            raise ValueError(
                "trading_mode=live requires BOTSENSAI_I_UNDERSTAND_THE_RISK=1. "
                "Refusing to start. Use paper mode."
            )
        if self.trading_mode is TradingMode.LIVE and self.dry_run:
            raise ValueError("trading_mode=live is incompatible with dry_run=true")
        return self

    @property
    def live_enabled(self) -> bool:
        return self.trading_mode is TradingMode.LIVE and self.i_understand_the_risk

    @property
    def x_session_enabled(self) -> bool:
        """Session collection needs three independent things to line up.

        The burner acknowledgement is deliberately separate from `enabled`: it
        makes "I have thought about which account this is" an explicit act rather
        than a side-effect of turning a feature on.
        """
        return bool(
            self.x_session.enabled
            and self.x_session.acknowledged_burner
            and self.browser.user_data_dir
        )

    def collector(self, name: str) -> CollectorSettings:
        return self.collectors.setdefault(name, CollectorSettings())

    def path(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else REPO_ROOT / p


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return data


def load_settings(config_path: str | Path | None = None, **overrides: Any) -> Settings:
    """Build Settings from YAML, then environment, then explicit overrides."""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    base = _load_yaml(path)
    base.update(overrides)
    return Settings(**base)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Call `get_settings.cache_clear()` in tests."""
    return load_settings(os.environ.get("BOTSENSAI_CONFIG"))


__all__ = [
    "BacktestSettings",
    "BrowserSettings",
    "CollectorSettings",
    "DEFAULT_CONFIG_PATH",
    "ExecutionSettings",
    "MediaHashSettings",
    "MediaSettings",
    "MemorySettings",
    "REPO_ROOT",
    "RiskSettings",
    "ScoringSettings",
    "Settings",
    "TradingMode",
    "get_settings",
    "load_settings",
]
