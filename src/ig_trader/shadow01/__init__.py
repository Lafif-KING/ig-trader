"""Prospective, observation-only Shadow Tournament V1.

This package deliberately has no broker-order or position-management API.
"""

from src.ig_trader.shadow01.config import ShadowTournamentConfig, load_config

__all__ = ("ShadowTournamentConfig", "load_config")
