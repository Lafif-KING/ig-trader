"""Offline broker-neutral SHADOW_DEMO execution core."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4


class ExecutionMode(StrEnum):
    NO_EXECUTION = "NO_EXECUTION"
    SHADOW_DEMO = "SHADOW_DEMO"
    DEMO_EXECUTION = "DEMO_EXECUTION"
    LIVE_EXECUTION = "LIVE_EXECUTION"


class ShadowLifecycle(StrEnum):
    SHADOW_INTENT_CREATED = "SHADOW_INTENT_CREATED"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    RECONCILED = "RECONCILED"
    FAILED_SAFE = "FAILED_SAFE"


class ShadowExecutionError(RuntimeError):
    """A shadow operation was rejected without authorizing execution."""


@dataclass(frozen=True)
class MarketQuote:
    bid: float
    offer: float
    as_of: datetime

    def valid(self, now: datetime, max_age: timedelta) -> bool:
        return (
            self.bid > 0
            and self.offer >= self.bid
            and self.as_of.tzinfo is not None
            and self.as_of.astimezone(UTC) <= now.astimezone(UTC)
            and now.astimezone(UTC) - self.as_of.astimezone(UTC) <= max_age
        )


@dataclass(frozen=True)
class ShadowIntentRecord:
    intent_id: UUID
    strategy_id: str
    instrument: str
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    fencing_token: int
    lifecycle: ShadowLifecycle = ShadowLifecycle.SHADOW_INTENT_CREATED
    exit_price: float | None = None
    exit_reason: str | None = None


class ShadowStore(Protocol):
    def get(self, intent_id: UUID) -> ShadowIntentRecord | None: ...

    def put(self, record: ShadowIntentRecord) -> ShadowIntentRecord: ...

    def transition(
        self,
        intent_id: UUID,
        lifecycle: ShadowLifecycle,
        fencing_token: int,
        *,
        exit_price: float | None = None,
        exit_reason: str | None = None,
    ) -> ShadowIntentRecord: ...


class InMemoryShadowStore:
    """Disposable deterministic store used by offline tests and local runs."""

    def __init__(self) -> None:
        self.records: dict[UUID, ShadowIntentRecord] = {}

    def get(self, intent_id: UUID) -> ShadowIntentRecord | None:
        return self.records.get(intent_id)

    def put(self, record: ShadowIntentRecord) -> ShadowIntentRecord:
        existing = self.records.get(record.intent_id)
        if existing is not None and existing != record:
            raise ShadowExecutionError("duplicate shadow intent conflicts")
        self.records[record.intent_id] = record
        return record

    def transition(
        self,
        intent_id: UUID,
        lifecycle: ShadowLifecycle,
        fencing_token: int,
        *,
        exit_price: float | None = None,
        exit_reason: str | None = None,
    ) -> ShadowIntentRecord:
        current = self.records.get(intent_id)
        if current is None or current.fencing_token != fencing_token:
            raise ShadowExecutionError("shadow transition is unknown or stale")
        allowed = {
            ShadowLifecycle.SHADOW_INTENT_CREATED: {ShadowLifecycle.OPEN},
            ShadowLifecycle.OPEN: {ShadowLifecycle.CLOSED, ShadowLifecycle.FAILED_SAFE},
            ShadowLifecycle.CLOSED: {ShadowLifecycle.RECONCILED},
        }
        if lifecycle not in allowed.get(current.lifecycle, set()):
            raise ShadowExecutionError("shadow lifecycle transition is invalid")
        updated = replace(
            current, lifecycle=lifecycle, exit_price=exit_price, exit_reason=exit_reason
        )
        self.records[intent_id] = updated
        return updated


class ShadowExecutionCore:
    """Risk-gated, leader-only hypothetical execution with no broker port."""

    def __init__(
        self,
        *,
        mode: ExecutionMode,
        lease: Any,
        store: ShadowStore,
        risk_gate: Any,
        max_quote_age: timedelta = timedelta(seconds=10),
    ) -> None:
        self.mode = mode
        self.lease = lease
        self.store = store
        self.risk_gate = risk_gate
        self.max_quote_age = max_quote_age

    @property
    def authorized(self) -> bool:
        return self.mode is ExecutionMode.SHADOW_DEMO and bool(self.lease.authorized)

    @property
    def order_authority(self) -> bool:
        return False

    def create_intent(
        self,
        signal: Any,
        quote: MarketQuote,
        *,
        intent_id: UUID | None = None,
        stop_price: float,
        target_price: float,
        open_positions_for_strategy: int,
        daily_loss_pct: float,
        now: datetime,
    ) -> ShadowIntentRecord:
        if not self.authorized or not quote.valid(now, self.max_quote_age):
            raise ShadowExecutionError("shadow creation rejected fail closed")
        try:
            approved = self.risk_gate(
                signal,
                open_positions_for_strategy=open_positions_for_strategy,
                daily_loss_pct=daily_loss_pct,
            )
        except Exception:
            raise ShadowExecutionError("portfolio risk failed closed") from None
        if not approved:
            raise ShadowExecutionError("portfolio risk vetoed shadow intent")
        direction = getattr(signal.direction, "value", signal.direction)
        if direction not in {"BUY", "SELL"}:
            raise ShadowExecutionError("shadow signal is not actionable")
        if stop_price <= 0 or target_price <= 0:
            raise ShadowExecutionError("shadow prices are invalid")
        token = int(self.lease.fencing_token)
        record = ShadowIntentRecord(
            intent_id=intent_id or uuid4(),
            strategy_id=str(signal.strategy_name),
            instrument=str(signal.epic),
            direction=direction,
            entry_price=quote.offer if direction == "BUY" else quote.bid,
            stop_price=stop_price,
            target_price=target_price,
            fencing_token=token,
        )
        existing = self.store.get(record.intent_id)
        if existing is not None:
            return existing
        created = self.store.put(record)
        return self.store.transition(created.intent_id, ShadowLifecycle.OPEN, token)

    def advance(
        self, position: ShadowIntentRecord, quote: MarketQuote, *, now: datetime
    ) -> ShadowIntentRecord:
        if not self.authorized or not quote.valid(now, self.max_quote_age):
            raise ShadowExecutionError("shadow advancement rejected fail closed")
        if position.lifecycle is not ShadowLifecycle.OPEN:
            return position
        exit_price: float | None = None
        reason: str | None = None
        if position.direction == "BUY":
            if quote.bid <= position.stop_price:
                exit_price, reason = quote.bid, "STOP"
            elif quote.bid >= position.target_price:
                exit_price, reason = quote.bid, "TARGET"
        else:
            if quote.offer >= position.stop_price:
                exit_price, reason = quote.offer, "STOP"
            elif quote.offer <= position.target_price:
                exit_price, reason = quote.offer, "TARGET"
        if reason is None:
            return position
        closed = self.store.transition(
            position.intent_id,
            ShadowLifecycle.CLOSED,
            int(self.lease.fencing_token),
            exit_price=exit_price,
            exit_reason=reason,
        )
        return self.store.transition(
            closed.intent_id,
            ShadowLifecycle.RECONCILED,
            int(self.lease.fencing_token),
            exit_price=exit_price,
            exit_reason=reason,
        )
