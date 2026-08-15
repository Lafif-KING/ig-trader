"""Structured logging configuration using structlog."""

import logging
import sys

import structlog


def configure_logging(log_level: str | None = None) -> None:
    """Configure structlog and standard logging."""

    if log_level is None:
        # Keep the existing public behavior for the legacy bot while allowing the
        # isolated cloud launcher to configure logging without resolving .env.
        from src.ig_trader.config import settings

        log_level = settings.log_level

    # Configure standard logging
    logging.basicConfig(
        format="%(message)s",
        level=log_level.upper(),
        stream=sys.stdout,
        force=True,
    )

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
