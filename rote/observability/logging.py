from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from typing import Any, TextIO

import structlog

SECRET_KEY_MARKERS = ("key", "token", "password", "secret", "authorization", "credential")
REDACTED = "[redacted]"


def configure_logging(stream: TextIO | None = None, level: int = logging.INFO) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _scrub_secrets,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


# a scrubber here is cheaper than trusting every call site to remember
def _scrub_secrets(
    _logger: Any, _method: str, event: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    for field in list(event):
        if any(marker in field.lower() for marker in SECRET_KEY_MARKERS):
            event[field] = REDACTED
    return event
