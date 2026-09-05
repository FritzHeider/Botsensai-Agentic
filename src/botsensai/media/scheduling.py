"""Scheduled content generation with deduplication and guardrails.

P5-02: Hourly recap and per-entry alerts written to the content directory.
Deduplicates against MemoryKind.OBSERVATION records so the same token is not
written up twice in a day (24 hours).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from botsensai.config import Settings, get_settings
from botsensai.media.generator import ContentGenerator
from botsensai.memory.store import MemoryStore
from botsensai.metrics import MetricRegistry, build_registry
from botsensai.models import ContentPiece, Launch, MemoryKind, Score, utcnow
from botsensai.store.db import Database


class ContentScheduler:
    """Schedules content generation (recaps & alerts) with 24h token deduplication."""

    def __init__(
        self,
        settings: Settings | None = None,
        db: Database | None = None,
        memory: MemoryStore | None = None,
        registry: MetricRegistry | None = None,
        generator: ContentGenerator | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db = db
        self.memory = memory or MemoryStore(self.settings.path(self.settings.memory.path))
        self.registry = registry or build_registry()
        self.generator = generator or ContentGenerator(
            settings=self.settings,
            registry=self.registry,
            memory=self.memory,
        )

    def is_recently_published(
        self,
        token_key: str,
        as_of: datetime | None = None,
        window_hours: float = 24.0,
    ) -> bool:
        """Check if a publication observation exists for token_key within window_hours."""
        now = as_of or utcnow()
        entries = self.memory.recall(
            subject=token_key,
            kinds=[MemoryKind.OBSERVATION],
            tags=["published"],
            as_of=now,
            limit=50,
            apply_decay=False,
        )
        cutoff = now - timedelta(hours=window_hours)
        return any(entry.created_at >= cutoff for entry in entries)

    def generate_hourly_recap(
        self,
        scores: Sequence[tuple[Launch, Score]],
        as_of: datetime | None = None,
        window_label: str = "the last hour",
        regime: str = "unknown",
        output_dir: str | Path | None = None,
    ) -> ContentPiece | None:
        """Generate and save an hourly recap across scored launches."""
        if not scores:
            return None
        piece = self.generator.recap(scores, window_label=window_label, regime=regime)
        self.generator.save(piece, directory=output_dir)
        self.generator.remember_publication(piece)
        return piece

    def generate_per_entry_alert(
        self,
        launch: Launch,
        score: Score,
        action: str = "BUY",
        as_of: datetime | None = None,
        window_hours: float = 24.0,
        output_dir: str | Path | None = None,
    ) -> ContentPiece | None:
        """Generate and save an alert for a launch, deduplicating within window_hours."""
        now = as_of or score.as_of
        if self.is_recently_published(launch.token.key, as_of=now, window_hours=window_hours):
            return None
        piece = self.generator.alert(launch, score, action=action)
        self.generator.save(piece, directory=output_dir)
        self.generator.remember_publication(piece)
        return piece

    def run_scheduled_cycle(
        self,
        scores: Sequence[tuple[Launch, Score]],
        as_of: datetime | None = None,
        window_hours: float = 1.0,
        dedup_window_hours: float = 24.0,
        output_dir: str | Path | None = None,
    ) -> list[ContentPiece]:
        """Run one scheduled cycle: generate hourly recap and alerts for eligible entries."""
        now = as_of or utcnow()
        published: list[ContentPiece] = []

        recap_piece = self.generate_hourly_recap(
            scores,
            as_of=now,
            window_label=f"the last {window_hours:g} hour(s)",
            output_dir=output_dir,
        )
        if recap_piece is not None:
            published.append(recap_piece)

        for launch, score in scores:
            if not score.vetoed and score.composite >= self.settings.scoring.entry_threshold:
                alert_piece = self.generate_per_entry_alert(
                    launch,
                    score,
                    action="BUY",
                    as_of=now,
                    window_hours=dedup_window_hours,
                    output_dir=output_dir,
                )
                if alert_piece is not None:
                    published.append(alert_piece)

        return published


__all__ = ["ContentScheduler"]
