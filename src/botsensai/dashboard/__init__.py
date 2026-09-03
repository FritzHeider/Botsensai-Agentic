"""Local dashboard: static HTML snapshots and real-time live WebSocket UI.

The panels here exist to make *silent* collection failures visible. Five bugs
found on 2026-07-29 all presented as a healthy system with nothing logged, and
two of them fabricated evidence rather than hiding it — so integrity comes
before presentation in this package.
"""

from botsensai.dashboard.data import build_snapshot
from botsensai.dashboard.integrity import IntegrityFlag, check_integrity
from botsensai.dashboard.render import render_html
from botsensai.dashboard.server import EventBroadcaster, broadcaster, create_app

__all__ = [
    "EventBroadcaster",
    "IntegrityFlag",
    "broadcaster",
    "build_snapshot",
    "check_integrity",
    "create_app",
    "render_html",
]
