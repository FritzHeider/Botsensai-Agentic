"""Botsensai — agentic memecoin recon, scoring, backtesting, paper trading and content.

Public entry points:

    from botsensai import Pipeline, Backtester, build_registry, CompositeScorer

Everything else is importable from its module. The package deliberately exposes
no function capable of signing or submitting a transaction.
"""

__version__ = "0.1.0"

from botsensai.config import Settings, TradingMode, get_settings, load_settings
from botsensai.metrics import MetricContext, MetricRegistry, build_registry, metric_catalogue

__all__ = [
    "MetricContext",
    "MetricRegistry",
    "Settings",
    "TradingMode",
    "__version__",
    "build_registry",
    "get_settings",
    "load_settings",
    "metric_catalogue",
]
