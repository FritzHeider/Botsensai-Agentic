"""Data collectors. Browser-driven (web-use) first, HTTP fast paths where they exist."""

from botsensai.collectors.base import CollectionResult, Collector, CollectorRegistry
from botsensai.collectors.browser import WebUseDriver, get_driver, shutdown_driver
from botsensai.collectors.dexscreener import DexscreenerCollector
from botsensai.collectors.geckoterminal import GeckoTerminalCollector
from botsensai.collectors.pumpfun import PumpFunCollector
from botsensai.collectors.social import (
    FourChanBizCollector,
    PumpFunChatCollector,
    RedditCollector,
    TelegramChannelCollector,
    XCollector,
)
from botsensai.collectors.x_session import AuthenticatedXCollector

#: Every collector the CLI knows how to build, in sweep order.
ALL_COLLECTORS = (
    PumpFunCollector,
    DexscreenerCollector,
    GeckoTerminalCollector,
    XCollector,
    RedditCollector,
    FourChanBizCollector,
    TelegramChannelCollector,
    PumpFunChatCollector,
)

__all__ = [
    "ALL_COLLECTORS",
    "AuthenticatedXCollector",
    "CollectionResult",
    "Collector",
    "CollectorRegistry",
    "DexscreenerCollector",
    "FourChanBizCollector",
    "GeckoTerminalCollector",
    "PumpFunChatCollector",
    "PumpFunCollector",
    "RedditCollector",
    "TelegramChannelCollector",
    "WebUseDriver",
    "XCollector",
    "get_driver",
    "shutdown_driver",
]
