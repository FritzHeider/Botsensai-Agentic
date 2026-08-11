"""Tests for scheduled content generation and deduplication (P5-02)."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from botsensai.config import Settings
from botsensai.media.generator import ContentGenerator
from botsensai.media.scheduling import ContentScheduler
from botsensai.memory.store import MemoryStore
from botsensai.models import (
    Chain,
    ContentPiece,
    Launch,
    Launchpad,
    MemoryKind,
    Score,
    TokenRef,
    VetoReason,
    utcnow,
)


def _make_token(symbol: str, mint_id: str) -> TokenRef:
    return TokenRef(chain=Chain.SOLANA, mint=f"{symbol}{mint_id}".ljust(44, "0")[:44])


def _make_launch(symbol: str, mint_id: str, created_at: datetime) -> Launch:
    return Launch(
        token=_make_token(symbol, mint_id),
        launchpad=Launchpad.PUMPFUN,
        created_at=created_at,
    )


def _make_score(token: TokenRef, as_of: datetime, composite: float = 0.75, vetoed: bool = False) -> Score:
    vetoes = [VetoReason.MINT_AUTHORITY_LIVE] if vetoed else []
    return Score(
        token=token,
        as_of=as_of,
        composite=composite,
        coverage=0.8,
        regime="normal",
        vetoes=vetoes,
    )


def test_hourly_recap_generation(tmp_path: Path):
    """Test generating and saving an hourly recap."""
    settings = Settings()
    mem_store = MemoryStore(tmp_path / "memory.db")
    scheduler = ContentScheduler(settings=settings, memory=mem_store)

    now = utcnow()
    launch1 = _make_launch("BONK", "1", now)
    score1 = _make_score(launch1.token, now, composite=0.72)
    launch2 = _make_launch("PEPE", "2", now)
    score2 = _make_score(launch2.token, now, composite=0.45)

    scores = [(launch1, score1), (launch2, score2)]

    piece = scheduler.generate_hourly_recap(scores, as_of=now, output_dir=tmp_path)

    assert piece is not None
    assert piece.kind == "recap"
    assert "Launch feed recap" in piece.title
    assert settings.media.disclosure_text in piece.body

    files = list(tmp_path.glob("*-recap-*.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "BONK" in content
    assert settings.media.disclosure_text in content


def test_per_entry_alert_generation(tmp_path: Path):
    """Test generating and saving a per-entry alert, recording publication in memory."""
    settings = Settings()
    mem_store = MemoryStore(tmp_path / "memory.db")
    scheduler = ContentScheduler(settings=settings, memory=mem_store)

    now = utcnow()
    launch = _make_launch("WIF", "1", now)
    score = _make_score(launch.token, now, composite=0.80)

    piece = scheduler.generate_per_entry_alert(launch, score, action="BUY", output_dir=tmp_path)

    assert piece is not None
    assert piece.kind == "alert"
    assert "BUY $WIF" in piece.title
    assert settings.media.disclosure_text in piece.body

    files = list(tmp_path.glob("*-alert-*.md"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "$WIF" in content

    # Check that MemoryStore recorded the publication
    obs = mem_store.recall(
        subject=launch.token.key,
        kinds=[MemoryKind.OBSERVATION],
        tags=["published"],
        as_of=utcnow(),
    )
    assert len(obs) == 1
    assert "published alert" in obs[0].title


def test_no_duplicate_publication_within_24h(tmp_path: Path):
    """Test that a token published once is NOT published again within 24 hours."""
    settings = Settings()
    mem_store = MemoryStore(tmp_path / "memory.db")
    scheduler = ContentScheduler(settings=settings, memory=mem_store)

    t0 = utcnow()
    launch = _make_launch("POPCAT", "1", t0)
    score = _make_score(launch.token, t0, composite=0.85)

    # First publication succeeds
    piece1 = scheduler.generate_per_entry_alert(launch, score, action="BUY", output_dir=tmp_path)
    assert piece1 is not None

    # Check is_recently_published at t0 + 2h
    t_2h = t0 + timedelta(hours=2)
    assert scheduler.is_recently_published(launch.token.key, as_of=t_2h, window_hours=24.0) is True

    # Second publication attempt at t0 + 2h is deduplicated and returns None
    piece2 = scheduler.generate_per_entry_alert(
        launch, score, action="BUY", as_of=t_2h, output_dir=tmp_path
    )
    assert piece2 is None

    # Verify only 1 file written
    files = list(tmp_path.glob("*-alert-*.md"))
    assert len(files) == 1


def test_publication_allowed_after_24h(tmp_path: Path):
    """Test that publication for a token IS allowed after 24 hours have elapsed."""
    settings = Settings()
    mem_store = MemoryStore(tmp_path / "memory.db")
    scheduler = ContentScheduler(settings=settings, memory=mem_store)

    t0 = utcnow()
    launch = _make_launch("MEW", "1", t0)
    score = _make_score(launch.token, t0, composite=0.82)

    # First publication at t0
    piece1 = scheduler.generate_per_entry_alert(launch, score, action="BUY", output_dir=tmp_path)
    assert piece1 is not None

    # Second publication at t0 + 25h
    t_25h = t0 + timedelta(hours=25)
    assert scheduler.is_recently_published(launch.token.key, as_of=t_25h, window_hours=24.0) is False

    piece2 = scheduler.generate_per_entry_alert(
        launch, score, action="BUY", as_of=t_25h, output_dir=tmp_path
    )
    assert piece2 is not None
    assert piece2.kind == "alert"


def test_disclosure_guardrails_enforced(tmp_path: Path):
    """Test that disclosure text is mandatory and forbidden phrasing raises ValueError."""
    generator = ContentGenerator()
    now = utcnow()
    launch = _make_launch("SCAM", "1", now)
    score = _make_score(launch.token, now, composite=0.90)

    # Generating a valid piece works
    piece = generator.alert(launch, score, action="BUY")
    assert generator.media.disclosure_text in piece.body

    # Mutating body to contain forbidden phrase raises ValueError during _enforce
    bad_piece = ContentPiece(
        token=launch.token,
        kind="alert",
        title="BAD ALERT",
        body=f"BUY $SCAM - guaranteed returns! {generator.media.disclosure_text}",
        disclosure=generator.media.disclosure_text,
    )
    with pytest.raises(ValueError, match="prohibited phrasing"):
        generator._enforce(bad_piece)

    # Missing disclosure raises ValueError
    no_disc_piece = ContentPiece(
        token=launch.token,
        kind="alert",
        title="NO DISC",
        body="BUY $SCAM score 0.90",
        disclosure="Required disclosure statement",
    )
    with pytest.raises(ValueError, match="missing its disclosure"):
        generator._enforce(no_disc_piece)


def test_run_scheduled_cycle(tmp_path: Path):
    """Test running a full scheduled cycle with recap and alerts."""
    settings = Settings()
    mem_store = MemoryStore(tmp_path / "memory.db")
    scheduler = ContentScheduler(settings=settings, memory=mem_store)

    now = utcnow()
    # High score token (should trigger alert)
    launch1 = _make_launch("HIGH", "1", now)
    score1 = _make_score(launch1.token, now, composite=0.75, vetoed=False)
    # Low score token (no alert)
    launch2 = _make_launch("LOW", "2", now)
    score2 = _make_score(launch2.token, now, composite=0.40, vetoed=False)
    # Vetoed high score token (no alert)
    launch3 = _make_launch("VETO", "3", now)
    score3 = _make_score(launch3.token, now, composite=0.85, vetoed=True)

    scores = [(launch1, score1), (launch2, score2), (launch3, score3)]

    published = scheduler.run_scheduled_cycle(scores, as_of=now, output_dir=tmp_path)

    # Expect 1 recap + 1 alert (for HIGH) = 2 pieces
    assert len(published) == 2
    kinds = [p.kind for p in published]
    assert "recap" in kinds
    assert "alert" in kinds

    # Second cycle immediately after: recap runs again, but alert for HIGH is skipped due to 24h dedup
    published_second = scheduler.run_scheduled_cycle(
        scores, as_of=now + timedelta(minutes=5), output_dir=tmp_path
    )
    assert len(published_second) == 1
    assert published_second[0].kind == "recap"
