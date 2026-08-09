"""Intraday Trend-Following Strategy."""

import pandas as pd
import ta

from src.ig_trader.models import Signal, SignalDirection
from src.ig_trader.strategies.base import BaseStrategy


class IntradayStrategy(BaseStrategy):
    """Slow entries based on EMA Crossovers."""

    def __init__(self, fast_ema: int = 50, slow_ema: int = 200):
        super().__init__(name="Intraday")
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema

    def generate_signal(self, epic: str, df: pd.DataFrame) -> Signal:
        df["ema_f"] = ta.trend.ema_indicator(df["close"], window=self.fast_ema)
        df["ema_s"] = ta.trend.ema_indicator(df["close"], window=self.slow_ema)

        latest = df.iloc[-1]
        direction = SignalDirection.WAIT

        if latest["ema_f"] > latest["ema_s"]:
            direction = SignalDirection.BUY
        elif latest["ema_f"] < latest["ema_s"]:
            direction = SignalDirection.SELL

        return Signal(
            epic=epic,
            direction=direction,
            timestamp=df.index[-1],
            price=latest["close"],
            strategy_name=self.name,
            metadata={"ema_f": latest["ema_f"], "ema_s": latest["ema_s"]},
        )
