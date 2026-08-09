from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class SignalDirection(Enum):
    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"


@dataclass
class StrategyConfig:
    name: str
    enabled: bool
    budget_percentage: float
    max_positions: int
    timeframe: str


@dataclass
class Signal:
    epic: str
    direction: SignalDirection
    timestamp: datetime
    price: float
    strategy_name: str
    confidence: float = 1.0
    metadata: dict = None
