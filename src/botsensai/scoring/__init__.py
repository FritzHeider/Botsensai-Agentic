"""Composite scoring, veto gates, and weight fitting."""

from botsensai.scoring.composite import (
    CompositeScorer,
    VetoEngine,
    Weights,
    kelly_fraction,
    score_to_size,
)
from botsensai.scoring.fit import (
    AblationReport,
    AblationRow,
    FitReport,
    TrainingExample,
    WeightFitter,
    ablation,
    spearman,
    top_decile_lift,
)

__all__ = [
    "AblationReport",
    "AblationRow",
    "CompositeScorer",
    "FitReport",
    "TrainingExample",
    "VetoEngine",
    "WeightFitter",
    "Weights",
    "ablation",
    "kelly_fraction",
    "score_to_size",
    "spearman",
    "top_decile_lift",
]
