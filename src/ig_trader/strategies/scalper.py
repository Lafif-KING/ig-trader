from __future__ import annotations

from math import isfinite

import pandas as pd

from src.ig_trader.indicators import add_adx, add_atr, add_rsi
from src.ig_trader.models import Signal, SignalDirection
from src.ig_trader.strategies.base import BaseStrategy


class ScalperStrategy(BaseStrategy):
    """Frozen V1 RSI/ADX Scalper with no execution authority."""

    def __init__(
        self,
        rsi_period: int = 7,
        minimum_confidence: float = 0.70,
        adx_threshold: float = 20.0,
        minimum_candles: int = 60,
    ) -> None:
        super().__init__(name="Scalper")
        if rsi_period != 7:
            raise ValueError("frozen Scalper RSI period must be 7")
        if minimum_confidence != 0.70:
            raise ValueError("frozen Scalper confidence threshold must be 0.70")
        if adx_threshold != 20.0:
            raise ValueError("frozen Scalper ADX threshold must be 20")
        if minimum_candles != 60:
            raise ValueError("frozen Scalper warm-up must be 60 candles")
        self.rsi_period = rsi_period
        self.minimum_confidence = minimum_confidence
        self.adx_threshold = adx_threshold
        self.minimum_candles = minimum_candles

    def generate_signal(self, epic: str, df: pd.DataFrame) -> Signal:
        """Return a broker-neutral signal from validated candle data."""

        if not isinstance(df, pd.DataFrame) or df.empty or len(df) < self.minimum_candles:
            timestamp = df.index[-1] if isinstance(df, pd.DataFrame) and not df.empty else pd.NaT
            price = (
                float(df["close"].iloc[-1])
                if isinstance(df, pd.DataFrame) and not df.empty
                else 0.0
            )
            return Signal(
                epic=epic,
                direction=SignalDirection.WAIT,
                timestamp=timestamp,
                price=price,
                strategy_name=self.name,
                confidence=0.0,
                metadata={"reason": "warmup_incomplete"},
            )
        required = {"open", "high", "low", "close"}
        if not required.issubset(df.columns):
            return Signal(
                epic=epic,
                direction=SignalDirection.WAIT,
                timestamp=df.index[-1],
                price=0.0,
                strategy_name=self.name,
                confidence=0.0,
                metadata={"reason": "candle_columns_missing"},
            )
        prepared = add_rsi(df.copy(), length=self.rsi_period)
        prepared = add_atr(prepared, length=14)
        prepared = add_adx(prepared, length=14)
        latest = prepared.iloc[-1]
        values = (latest["rsi"], latest["atr"], latest["adx"])
        if any(not isfinite(float(value)) for value in values):
            return Signal(
                epic=epic,
                direction=SignalDirection.WAIT,
                timestamp=prepared.index[-1],
                price=float(latest["close"]),
                strategy_name=self.name,
                confidence=0.0,
                metadata={"reason": "indicators_not_ready"},
            )
        rsi = float(latest["rsi"])
        adx = float(latest["adx"])
        atr = float(latest["atr"])
        direction = SignalDirection.WAIT
        rsi_strength = 0.0
        if rsi < 30:
            rsi_strength = min((30.0 - rsi) / 30.0, 1.0)
            proposed = SignalDirection.BUY
        elif rsi > 70:
            rsi_strength = min((rsi - 70.0) / 30.0, 1.0)
            proposed = SignalDirection.SELL
        else:
            proposed = SignalDirection.WAIT
        adx_strength = min(adx / 40.0, 1.0)
        confidence = round(0.70 * rsi_strength + 0.30 * adx_strength, 4)
        if (
            proposed is not SignalDirection.WAIT
            and adx >= self.adx_threshold
            and confidence >= self.minimum_confidence
        ):
            direction = proposed
        return Signal(
            epic=epic,
            direction=direction,
            timestamp=prepared.index[-1],
            price=float(latest["close"]),
            strategy_name=self.name,
            confidence=confidence,
            metadata={
                "rsi": round(rsi, 4),
                "adx": round(adx, 4),
                "atr": atr,
                "rsi_strength": round(rsi_strength, 4),
                "adx_strength": round(adx_strength, 4),
            },
        )
