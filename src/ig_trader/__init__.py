"""IG Trader package without import-time configuration or database mutation."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.ig_trader.config import settings
    from src.ig_trader.database import init_db
    from src.ig_trader.logging_config import configure_logging

__version__ = "0.1.0"
__all__ = ["settings", "configure_logging", "init_db"]


def __getattr__(name: str) -> Any:
    """Retain public imports while avoiding implicit startup side effects."""

    modules = {
        "settings": "config",
        "configure_logging": "logging_config",
        "init_db": "database",
    }
    module = modules.get(name)
    if module is None:
        raise AttributeError(name)
    value = getattr(import_module(f"src.ig_trader.{module}"), name)
    globals()[name] = value
    return value
