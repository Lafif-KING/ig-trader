"""IG Trader — AI-powered automated trading platform."""

__version__ = "0.1.0"

from src.ig_trader.config import settings
from src.ig_trader.database import init_db
from src.ig_trader.logging_config import configure_logging

# 1. Configure logging
configure_logging()

# 2. Initialize the database
init_db()

__all__ = ["settings", "configure_logging", "init_db"]
