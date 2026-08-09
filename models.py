"""Domain models for the trading platform."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class SignalDirection(Enum):
    """Possible directions for a trade signal."""

    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"


@dataclass
class StrategyConfig:
    """Configuration for budget and risk per strategy."""

    name: str
    enabled: bool
    budget_percentage: float  # e.g., 0.30 for 30%
    max_positions: int
    timeframe: str  # e.g., "MINUTE" or "HOUR"


@dataclass
class Signal:
    """A trade signal produced by a strategy."""

    epic: str
    direction: SignalDirection
    timestamp: datetime
    price: float
    strategy_name: str
    confidence: float = 1.0
    metadata: dict = None
