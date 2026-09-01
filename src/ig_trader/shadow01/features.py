"""Causal feature-builder boundary for Shadow Tournament decisions.

This module deliberately has no outcome-label or storage import.  It can see
only a market snapshot built from completed observations.
"""

from __future__ import annotations

from src.ig_trader.shadow01.config import ShadowTournamentConfig
from src.ig_trader.shadow01.engines import compute_technical_state
from src.ig_trader.shadow01.models import MarketSnapshot, TechnicalState


class CausalFeatureBuilder:
    """Build decision-time state only from a completed market snapshot."""

    def __init__(self, config: ShadowTournamentConfig) -> None:
        self._config = config

    def build(self, snapshot: MarketSnapshot) -> TechnicalState:
        return compute_technical_state(snapshot, self._config)
