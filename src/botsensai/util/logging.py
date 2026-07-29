"""Structured logging setup.

One configuration point for the whole system. Collectors, metrics, the scorer
and the broker all bind context (token key, surface name, run id) so that a
single trade decision can be reconstructed from the log alone.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_CONFIGURED = False


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Idempotent logging setup. Safe to call from tests and from the CLI."""
    global _CONFIGURED

    numeric = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=numeric,
        force=True,
    )
    # These libraries are chatty at DEBUG and drown out our own events.
    for noisy in ("httpx", "httpcore", "asyncio", "urllib3", "websockets"):
        logging.getLogger(noisy).setLevel(max(numeric, logging.WARNING))

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]
    if json_output:
        processors.append(structlog.processors.format_exc_info)
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str, **initial: Any) -> Any:
    """Return a bound structlog logger, configuring on first use."""
    if not _CONFIGURED:
        configure_logging()
    logger = structlog.get_logger(name)
    return logger.bind(**initial) if initial else logger


def bind_run(run_id: str, **extra: Any) -> None:
    """Bind a run identifier to every subsequent log line in this context."""
    structlog.contextvars.bind_contextvars(run_id=run_id, **extra)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


__all__ = ["bind_run", "clear_context", "configure_logging", "get_logger"]
