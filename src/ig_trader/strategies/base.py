from abc import ABC, abstractmethod

import pandas as pd

from src.ig_trader.models import Signal


class BaseStrategy(ABC):
    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def generate_signal(self, epic: str, df: pd.DataFrame) -> Signal:
        pass
