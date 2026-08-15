"""Broker-neutral ports used by the exact OFFLINE_PAPER conductor."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from src.ig_trader.offline_paper.domain import (
    AccountSnapshot,
    BrokerOrder,
    Candle,
    Exit,
    Fill,
    Position,
    Quote,
    ReconciliationSnapshot,
)


@runtime_checkable
class MarketDataPort(Protocol):
    def quote(self, epic: str, *, as_of: datetime) -> Quote | None: ...


@runtime_checkable
class HistoricalDataPort(Protocol):
    def candles(self, epic: str, *, before: datetime) -> tuple[Candle, ...] | None: ...

    def exit_candle(self, epic: str, *, after: datetime) -> Candle | None: ...


@runtime_checkable
class ExecutionPort(Protocol):
    def submit(self, order: BrokerOrder) -> Fill: ...

    def close(self, request: Exit) -> Exit | None: ...


@runtime_checkable
class AccountPort(Protocol):
    def account_snapshot(self, *, as_of: datetime) -> AccountSnapshot | None: ...


@runtime_checkable
class ReconciliationPort(Protocol):
    def order_for_intent(self, intent_id: str) -> BrokerOrder | None: ...

    def fill_for_intent(self, intent_id: str) -> Fill | None: ...

    def position_for_intent(self, intent_id: str) -> Position | None: ...

    def exit_for_intent(self, intent_id: str) -> Exit | None: ...

    def reconciliation_snapshot(self, *, as_of: datetime) -> ReconciliationSnapshot | None: ...
