"""IG Trader — AI-powered automated trading platform."""

__version__ = "0.1.0"

from src.ig_trader.config import settings
from src.ig_trader.logging_config import configure_logging

# Configure logging on import
configure_logging()

__all__ = ["settings", "configure_logging"]
