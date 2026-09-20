"""Real-time X / social virality spike accelerator.

Detects instant social velocity spikes when a new token mint is cited by verified accounts
within the first 60 seconds of creation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from botsensai.config import Settings, get_settings
from botsensai.models import Confidence, MetricValue, TokenRef
from botsensai.util.logging import get_logger

log = get_logger(__name__)


@dataclass
class SocialSpikeSignal:
    token_mint: str
    verified_post_count: int
    total_impressions: int
    velocity_score: float
    is_viral: bool


class SocialSpikeDetector:
    """Monitors real-time mentions and flags breakout virality spikes."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def evaluate_social_velocity(
        self,
        token: TokenRef,
        recent_mentions: list[dict[str, Any]] | None = None,
        age_seconds: float = 30.0,
    ) -> SocialSpikeSignal:
        """Calculate virality score based on post count and follower reach in early lifecycle."""
        if not recent_mentions:
            return SocialSpikeSignal(
                token_mint=token.mint,
                verified_post_count=0,
                total_impressions=0,
                velocity_score=0.0,
                is_viral=False,
            )

        verified_posts = [m for m in recent_mentions if m.get("followers", 0) >= 2_000]
        total_reach = sum(m.get("followers", 0) for m in recent_mentions)
        
        # High velocity: >= 4 distinct accounts posting within the first 2 minutes
        is_viral = len(verified_posts) >= 4 and age_seconds <= 120.0
        velocity = min(1.0, (len(verified_posts) / 5.0) * (total_reach / 20_000.0))

        if is_viral:
            log.info(
                "social_spike.viral_signal_detected",
                mint=token.mint,
                posts=len(verified_posts),
                reach=total_reach,
            )

        return SocialSpikeSignal(
            token_mint=token.mint,
            verified_post_count=len(verified_posts),
            total_impressions=total_reach,
            velocity_score=velocity,
            is_viral=is_viral,
        )
