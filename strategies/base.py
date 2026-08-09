"""Base class for all trading strategies."""

from abc import ABC, abstractmethod

import pandas as pd

from src.ig_trader.models import Signal


class BaseStrategy(ABC):
    """Abstract base class that all strategies must inherit from."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def generate_signal(self, epic: str, df: pd.DataFrame) -> Signal:
        """Analyze data and return a Signal."""
        pass
