"""Local dashboard: a static HTML build over the existing store.

The panels here exist to make *silent* collection failures visible. Five bugs
found on 2026-07-29 all presented as a healthy system with nothing logged, and
two of them fabricated evidence rather than hiding it — so integrity comes
before presentation in this package.
"""

from botsensai.dashboard.integrity import IntegrityFlag, check_integrity

__all__ = ["IntegrityFlag", "check_integrity"]
