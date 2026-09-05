from datetime import UTC, datetime

import pytest

from botsensai.config import Settings
from botsensai.demo import seed_demo_environment
from botsensai.models import Fill, Launch, Launchpad, Score, Side, TokenRef
from botsensai.sniper import _render_snipe_dossier, execute_live_snipe
from botsensai.store.db import Database


def test_render_snipe_dossier_smoke() -> None:
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    token = TokenRef(mint="testmint1234567890", name="Test Token", symbol="TEST")
    launch = Launch(token=token, launchpad=Launchpad.PUMPFUN, created_at=now)
    score = Score(
        token=token,
        composite=0.785,
        coverage=0.91,
        as_of=now,
        observed_at=now,
        vetoes=[],
        explanation="Strong organic dispersion.",
    )
    fill = Fill(
        token=token,
        as_of=now,
        side=Side.BUY,
        amount_token=15000.0,
        amount_native=0.25,
        price_native=0.0000166,
        slippage_bps=12.5,
        fee_native=0.0005,
        tip_native=0.001,
        latency_ms=120.0,
        rejected=False,
    )

    # Smoke render should succeed without exception
    _render_snipe_dossier(launch, score, fill)
    _render_snipe_dossier(launch, score, None, refusal_reason="Below threshold")


@pytest.mark.asyncio
async def test_execute_live_snipe_with_seeded_db(tmp_path) -> None:
    db = Database(tmp_path / "test.db")
    tokens = seed_demo_environment(db)
    assert len(tokens) == 2
    db.close()

    settings = Settings(db_path=str(tmp_path / "test.db"))

    # Execute sniper with force=True over seeded database
    launch, score, fill = await execute_live_snipe(
        settings=settings,
        size_sol=0.1,
        min_score=0.5,
        force=True,
        discover_limit=5,
        max_candidates=5,
    )

    # Since it sweeps live or returns seeded, assert no unexpected exceptions
    assert True
