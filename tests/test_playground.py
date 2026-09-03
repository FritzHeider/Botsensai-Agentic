"""Unit tests for interactive scenario training playground."""

import pytest

from botsensai.playground import CURATED_SCENARIOS, evaluate_decision, run_playground


def test_evaluate_decision() -> None:
    scen = CURATED_SCENARIOS[0]
    # Optimal decision for SCEN-01 is 'veto'
    is_corr, feedback = evaluate_decision(scen, "veto")
    assert is_corr is True
    assert "EXCELLENT" in feedback

    is_incorr, feedback_bad = evaluate_decision(scen, "enter")
    assert is_incorr is False
    assert "SUB-OPTIMAL" in feedback_bad


@pytest.mark.asyncio
async def test_run_playground_auto() -> None:
    score = await run_playground(interactive=False)
    assert score >= 0
