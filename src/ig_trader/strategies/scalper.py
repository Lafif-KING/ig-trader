import pandas as pd

from src.ig_trader.indicators import add_rsi
from src.ig_trader.models import Signal, SignalDirection
from src.ig_trader.strategies.base import BaseStrategy


class ScalperStrategy(BaseStrategy):
    def __init__(self, rsi_period: int = 7):
        super().__init__(name="Scalper")
        self.rsi_period = rsi_period

    def generate_signal(self, epic: str, df: pd.DataFrame) -> Signal:
        df = add_rsi(df, length=self.rsi_period)
        latest = df.iloc[-1]
        direction = SignalDirection.WAIT
        if latest["rsi"] < 30:
            direction = SignalDirection.BUY
        elif latest["rsi"] > 70:
            direction = SignalDirection.SELL
        return Signal(
            epic=epic,
            direction=direction,
            timestamp=df.index[-1],
            price=latest["close"],
            strategy_name=self.name,
            metadata={"rsi": round(latest["rsi"], 2)},
        )
