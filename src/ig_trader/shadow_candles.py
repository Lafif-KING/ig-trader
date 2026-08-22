"""Bounded UTC one-minute finalized-candle assembly for Shadow market reads."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite


@dataclass(frozen=True)
class FinalCandle:
    epic: str
    minute: datetime
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    offer_open: float
    offer_high: float
    offer_low: float
    offer_close: float


@dataclass(frozen=True)
class QuoteTick:
    epic: str
    bid: float
    offer: float
    timestamp: datetime


class FinalMinuteCandleBuilder:
    """Never fills unknown gaps and emits at most one completed prior minute."""

    def __init__(self, epic: str, *, capacity: int = 60) -> None:
        if not epic or capacity < 60:
            raise ValueError("Shadow candle builder requires a bounded 60-candle capacity")
        self.epic = epic
        self._capacity = capacity
        self._candles: deque[FinalCandle] = deque(maxlen=capacity)
        self._minute: datetime | None = None
        self._ticks: list[QuoteTick] = []

    @property
    def finalized(self) -> tuple[FinalCandle, ...]:
        return tuple(self._candles)

    def add(self, tick: QuoteTick) -> FinalCandle | None:
        if tick.epic != self.epic or not _valid_tick(tick):
            raise ValueError("Shadow quote tick is invalid")
        minute = tick.timestamp.astimezone(UTC).replace(second=0, microsecond=0)
        if self._minute is None:
            self._minute = minute
        if minute < self._minute:
            raise ValueError("Shadow quote timestamps must be monotonic")
        if minute == self._minute:
            self._ticks.append(tick)
            return None
        if minute != self._minute + timedelta(minutes=1):
            self._ticks = [tick]
            self._minute = minute
            return None
        candle = _finalize(self.epic, self._minute, self._ticks)
        self._candles.append(candle)
        self._minute = minute
        self._ticks = [tick]
        return candle


def _finalize(epic: str, minute: datetime, ticks: list[QuoteTick]) -> FinalCandle:
    if not ticks:
        raise ValueError("cannot synthesize a missing Shadow candle")
    return FinalCandle(
        epic=epic,
        minute=minute,
        bid_open=ticks[0].bid,
        bid_high=max(tick.bid for tick in ticks),
        bid_low=min(tick.bid for tick in ticks),
        bid_close=ticks[-1].bid,
        offer_open=ticks[0].offer,
        offer_high=max(tick.offer for tick in ticks),
        offer_low=min(tick.offer for tick in ticks),
        offer_close=ticks[-1].offer,
    )


def _valid_tick(tick: QuoteTick) -> bool:
    return (
        isinstance(tick.timestamp, datetime)
        and tick.timestamp.tzinfo is not None
        and all(isinstance(value, int | float) and isfinite(value) and value > 0 for value in (tick.bid, tick.offer))
        and tick.offer >= tick.bid
    )
