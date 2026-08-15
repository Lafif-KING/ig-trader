"""Canonical broker-neutral objects for the OFFLINE_PAPER lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class ExecutionMode(StrEnum):
    OFFLINE_PAPER = "OFFLINE_PAPER"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class LifecycleState(StrEnum):
    SIGNAL_DETECTED = "SIGNAL_DETECTED"
    RISK_REJECTED = "RISK_REJECTED"
    INTENT_CREATED = "INTENT_CREATED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACCEPTED = "ORDER_ACCEPTED"
    ORDER_REJECTED = "ORDER_REJECTED"
    POSITION_OPEN = "POSITION_OPEN"
    EXIT_REQUESTED = "EXIT_REQUESTED"
    POSITION_CLOSED = "POSITION_CLOSED"
    RECONCILED = "RECONCILED"
    FAILED_SAFE = "FAILED_SAFE"


class RunStatus(StrEnum):
    COMPLETE = "COMPLETE"
    NO_TRADE = "NO_TRADE"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class Quote:
    epic: str
    bid: float
    offer: float
    timestamp: datetime
    pip_size: float
    pip_value_account_currency: float
    minimum_size: float
    minimum_stop_pips: float

    @property
    def spread_pips(self) -> float:
        return (self.offer - self.bid) / self.pip_size


@dataclass(frozen=True)
class Candle:
    epic: str
    timestamp: datetime
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    offer_open: float
    offer_high: float
    offer_low: float
    offer_close: float
    volume: float

    @property
    def open(self) -> float:
        return (self.bid_open + self.offer_open) / 2.0

    @property
    def high(self) -> float:
        return (self.bid_high + self.offer_high) / 2.0

    @property
    def low(self) -> float:
        return (self.bid_low + self.offer_low) / 2.0

    @property
    def close(self) -> float:
        return (self.bid_close + self.offer_close) / 2.0


@dataclass(frozen=True)
class Signal:
    epic: str
    side: Side | None
    timestamp: datetime
    reference_price: float
    strategy: str
    strategy_version: str
    confidence: float
    inputs: dict[str, Any]


@dataclass(frozen=True)
class TradeCandidate:
    candidate_id: str
    cycle_id: str
    signal: Signal
    quote: Quote
    source_candle_references: tuple[dict[str, Any], ...]
    source_fingerprint: str


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    code: str
    account_equity: float | None
    daily_loss_pct: float | None
    current_positions: int | None
    projected_positions: int | None
    executions_in_cycle: int | None
    monetary_risk: float | None
    size: float | None
    stop_pips: float | None
    target_pips: float | None


@dataclass(frozen=True)
class TradeIntent:
    intent_id: str
    created_at: datetime
    cycle_id: str
    candidate_id: str
    epic: str
    side: Side
    strategy: str
    strategy_version: str
    signal_inputs: dict[str, Any]
    confidence: float
    spread_pips: float
    risk_decision: RiskDecision
    size: float
    stop_level: float
    target_level: float
    source_candle_references: tuple[dict[str, Any], ...]
    source_fingerprint: str
    execution_mode: ExecutionMode
    lifecycle_state: LifecycleState


@dataclass(frozen=True)
class BrokerOrder:
    order_reference: str
    intent_id: str
    epic: str
    side: Side
    size: float
    requested_price: float
    stop_level: float
    target_level: float
    pip_size: float
    pip_value_account_currency: float
    submitted_at: datetime


@dataclass(frozen=True)
class Fill:
    fill_reference: str
    order_reference: str
    position_reference: str | None
    accepted: bool
    reason: str
    price: float | None
    size: float | None
    timestamp: datetime


@dataclass(frozen=True)
class Position:
    position_reference: str
    intent_id: str
    epic: str
    side: Side
    size: float
    entry_price: float
    stop_level: float
    target_level: float
    pip_size: float
    pip_value_account_currency: float
    opened_at: datetime


@dataclass(frozen=True)
class Exit:
    exit_reference: str
    position_reference: str
    intent_id: str
    price: float
    reason: str
    profit_loss: float
    closed_at: datetime


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    currency: str
    balance: float
    starting_balance: float
    positions: tuple[Position, ...]
    captured_at: datetime
    state_known: bool

    @property
    def daily_loss_pct(self) -> float | None:
        if not self.state_known or self.starting_balance <= 0:
            return None
        return min((self.balance - self.starting_balance) / self.starting_balance, 0.0)


@dataclass(frozen=True)
class ReconciliationSnapshot:
    account: AccountSnapshot
    orders: tuple[BrokerOrder, ...]
    fills: tuple[Fill, ...]
    exits: tuple[Exit, ...]


@dataclass(frozen=True)
class LifecycleEvent:
    sequence: int
    intent_id: str
    from_state: LifecycleState | None
    to_state: LifecycleState
    reason: str
    occurred_at: datetime


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    reason: str
    cycle_id: str
    intent_ids: tuple[str, ...]
    lifecycle_states: tuple[LifecycleState, ...]
    risk_code: str
    paper_broker_result: str
    reconciliation_result: str
    idempotent_restart: bool
