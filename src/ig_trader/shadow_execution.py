"""Broker-neutral, risk-gated SHADOW_DEMO domain core."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5


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
    """A shadow operation was rejected without broker authority."""


@dataclass(frozen=True)
class InstrumentMetadata:
    epic: str
    pip_size: float

    def __post_init__(self) -> None:
        if not self.epic.strip() or not _positive_finite(self.pip_size):
            raise ShadowExecutionError("instrument metadata is invalid")


class InstrumentRegistry:
    """Injected frozen V1 instrument metadata."""

    def __init__(self, instruments: Mapping[str, InstrumentMetadata]) -> None:
        self._instruments = dict(instruments)

    def require(self, epic: str) -> InstrumentMetadata:
        metadata = self._instruments.get(epic)
        if metadata is None:
            raise ShadowExecutionError("instrument is outside frozen Shadow V1 scope")
        return metadata

    @classmethod
    def frozen_v1(cls) -> InstrumentRegistry:
        return cls(
            {
                epic: InstrumentMetadata(epic, 0.0001)
                for epic in (
                    "CS.D.EURGBP.MINI.IP",
                    "CS.D.EURUSD.MINI.IP",
                    "CS.D.GBPUSD.MINI.IP",
                )
            }
        )


@dataclass(frozen=True)
class MarketQuote:
    bid: float
    offer: float
    as_of: datetime

    def validate(self, now: datetime, max_age: timedelta) -> None:
        now_utc = _required_utc(now)
        as_of_utc = _required_utc(self.as_of)
        if (
            not _positive_finite(self.bid)
            or not _positive_finite(self.offer)
            or self.offer < self.bid
            or as_of_utc > now_utc
            or now_utc - as_of_utc > max_age
        ):
            raise ShadowExecutionError("market quote is missing, stale, or ambiguous")


@dataclass(frozen=True)
class ShadowIntentRecord:
    shadow_position_id: UUID
    intent_id: UUID
    strategy_id: str
    instrument: str
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    fencing_token: int
    created_at: datetime
    updated_at: datetime
    lifecycle: ShadowLifecycle = ShadowLifecycle.SHADOW_INTENT_CREATED
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None


@dataclass(frozen=True)
class ShadowPerformance:
    direction: str
    entry_price: float
    exit_price: float
    raw_price_delta: float
    pips: float
    r_multiple: float
    exit_reason: str
    opened_at: datetime
    closed_at: datetime


class ShadowStore(Protocol):
    def get(self, intent_id: UUID) -> ShadowIntentRecord | None: ...

    def active_position_count(self) -> int: ...

    def put(self, record: ShadowIntentRecord) -> ShadowIntentRecord: ...

    def transition(
        self,
        intent_id: UUID,
        from_state: ShadowLifecycle,
        to_state: ShadowLifecycle,
        fencing_token: int,
        *,
        updated_at: datetime,
        opened_at: datetime | None = None,
        closed_at: datetime | None = None,
        exit_price: float | None = None,
        exit_reason: str | None = None,
    ) -> ShadowIntentRecord: ...


class InMemoryShadowStore:
    """Disposable deterministic store with an authoritative current fence."""

    def __init__(self, current_fencing_token: int) -> None:
        self.current_fencing_token = current_fencing_token
        self.records: dict[UUID, ShadowIntentRecord] = {}

    def set_current_fencing_token(self, fencing_token: int) -> None:
        self.current_fencing_token = fencing_token

    def get(self, intent_id: UUID) -> ShadowIntentRecord | None:
        return self.records.get(intent_id)

    def active_position_count(self) -> int:
        return sum(
            record.lifecycle
            in {
                ShadowLifecycle.SHADOW_INTENT_CREATED,
                ShadowLifecycle.OPEN,
                ShadowLifecycle.FAILED_SAFE,
            }
            for record in self.records.values()
        )

    def put(self, record: ShadowIntentRecord) -> ShadowIntentRecord:
        self._require_fence(record.fencing_token)
        existing = self.records.get(record.intent_id)
        if existing is not None and _intent_identity(existing) != _intent_identity(record):
            raise ShadowExecutionError("duplicate shadow intent conflicts")
        self.records[record.intent_id] = existing or record
        return self.records[record.intent_id]

    def transition(
        self,
        intent_id: UUID,
        from_state: ShadowLifecycle,
        to_state: ShadowLifecycle,
        fencing_token: int,
        *,
        updated_at: datetime,
        opened_at: datetime | None = None,
        closed_at: datetime | None = None,
        exit_price: float | None = None,
        exit_reason: str | None = None,
    ) -> ShadowIntentRecord:
        self._require_fence(fencing_token)
        current = self.records.get(intent_id)
        if current is None or current.lifecycle is not from_state:
            raise ShadowExecutionError("shadow transition is unknown or conflicting")
        allowed = {
            ShadowLifecycle.SHADOW_INTENT_CREATED: {ShadowLifecycle.OPEN},
            ShadowLifecycle.OPEN: {ShadowLifecycle.CLOSED, ShadowLifecycle.FAILED_SAFE},
            ShadowLifecycle.CLOSED: {ShadowLifecycle.RECONCILED},
        }
        if to_state not in allowed.get(from_state, set()):
            raise ShadowExecutionError("shadow lifecycle transition is invalid")
        updated = replace(
            current,
            lifecycle=to_state,
            fencing_token=fencing_token,
            updated_at=_required_utc(updated_at),
            opened_at=opened_at if opened_at is not None else current.opened_at,
            closed_at=closed_at if closed_at is not None else current.closed_at,
            exit_price=exit_price if exit_price is not None else current.exit_price,
            exit_reason=exit_reason if exit_reason is not None else current.exit_reason,
        )
        self.records[intent_id] = updated
        return updated

    def _require_fence(self, fencing_token: int) -> None:
        if fencing_token != self.current_fencing_token:
            raise ShadowExecutionError("stale shadow fencing token")


class ShadowExecutionCore:
    """Leader-only hypothetical execution with permanently false broker authority."""

    def __init__(
        self,
        *,
        mode: ExecutionMode,
        lease: Any,
        store: ShadowStore,
        risk_gate: Any,
        instruments: InstrumentRegistry,
        max_quote_age: timedelta = timedelta(seconds=10),
    ) -> None:
        self.mode = mode
        self.lease = lease
        self.store = store
        self.risk_gate = risk_gate
        self.instruments = instruments
        self.max_quote_age = max_quote_age

    @property
    def authorized(self) -> bool:
        """Broker execution authorization is always false in this core."""

        return False

    @property
    def order_authority(self) -> bool:
        return False

    @property
    def can_advance_shadow(self) -> bool:
        return self.mode is ExecutionMode.SHADOW_DEMO and bool(self.lease.authorized)

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
        self._require_shadow_leader()
        quote.validate(now, self.max_quote_age)
        metadata = self.instruments.require(str(signal.epic))
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
        entry_price = quote.offer if direction == "BUY" else quote.bid
        _validate_geometry(direction, entry_price, stop_price, target_price)
        now_utc = _required_utc(now)
        token = self._fencing_token()
        resolved_intent_id = intent_id or uuid4()
        record = ShadowIntentRecord(
            shadow_position_id=uuid5(NAMESPACE_URL, f"ig-trader-shadow:{resolved_intent_id}"),
            intent_id=resolved_intent_id,
            strategy_id=str(signal.strategy_name),
            instrument=metadata.epic,
            direction=direction,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            fencing_token=token,
            created_at=now_utc,
            updated_at=now_utc,
        )
        existing = self.store.get(record.intent_id)
        if existing is not None:
            if _intent_identity(existing) != _intent_identity(record):
                raise ShadowExecutionError("duplicate shadow intent conflicts")
            return existing
        return self.store.put(record)

    def open_intent(self, record: ShadowIntentRecord, *, now: datetime) -> ShadowIntentRecord:
        self._require_shadow_leader()
        if record.lifecycle is ShadowLifecycle.OPEN:
            return record
        return self.store.transition(
            record.intent_id,
            ShadowLifecycle.SHADOW_INTENT_CREATED,
            ShadowLifecycle.OPEN,
            self._fencing_token(),
            updated_at=now,
            opened_at=_required_utc(now),
        )

    def close_on_quote(
        self,
        position: ShadowIntentRecord,
        quote: MarketQuote,
        *,
        now: datetime,
    ) -> ShadowIntentRecord:
        self._require_shadow_leader()
        quote.validate(now, self.max_quote_age)
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
        return self.store.transition(
            position.intent_id,
            ShadowLifecycle.OPEN,
            ShadowLifecycle.CLOSED,
            self._fencing_token(),
            updated_at=now,
            closed_at=_required_utc(now),
            exit_price=exit_price,
            exit_reason=reason,
        )

    def reconcile(self, position: ShadowIntentRecord, *, now: datetime) -> ShadowIntentRecord:
        self._require_shadow_leader()
        if position.lifecycle is ShadowLifecycle.RECONCILED:
            return position
        if position.lifecycle is not ShadowLifecycle.CLOSED:
            raise ShadowExecutionError("only a closed shadow position can reconcile")
        return self.store.transition(
            position.intent_id,
            ShadowLifecycle.CLOSED,
            ShadowLifecycle.RECONCILED,
            self._fencing_token(),
            updated_at=now,
        )

    def performance(self, position: ShadowIntentRecord) -> ShadowPerformance:
        if (
            position.lifecycle not in {ShadowLifecycle.CLOSED, ShadowLifecycle.RECONCILED}
            or position.exit_price is None
            or position.exit_reason is None
            or position.opened_at is None
            or position.closed_at is None
        ):
            raise ShadowExecutionError("closed shadow performance is unavailable")
        metadata = self.instruments.require(position.instrument)
        raw_delta = (
            position.exit_price - position.entry_price
            if position.direction == "BUY"
            else position.entry_price - position.exit_price
        )
        risk_distance = abs(position.entry_price - position.stop_price)
        return ShadowPerformance(
            direction=position.direction,
            entry_price=position.entry_price,
            exit_price=position.exit_price,
            raw_price_delta=raw_delta,
            pips=raw_delta / metadata.pip_size,
            r_multiple=raw_delta / risk_distance,
            exit_reason=position.exit_reason,
            opened_at=position.opened_at,
            closed_at=position.closed_at,
        )

    def _require_shadow_leader(self) -> None:
        if not self.can_advance_shadow:
            raise ShadowExecutionError("shadow advancement is not authorized")

    def _fencing_token(self) -> int:
        token = getattr(self.lease, "fencing_token", None)
        if token is None and getattr(self.lease, "lease", None) is not None:
            token = self.lease.lease.fencing_token
        if not isinstance(token, int) or token <= 0:
            raise ShadowExecutionError("shadow fencing token is unavailable")
        return token


def _intent_identity(record: ShadowIntentRecord) -> tuple[object, ...]:
    return (
        record.intent_id,
        record.strategy_id,
        record.instrument,
        record.direction,
        record.entry_price,
        record.stop_price,
        record.target_price,
    )


def _validate_geometry(direction: str, entry: float, stop: float, target: float) -> None:
    if not all(_positive_finite(value) for value in (entry, stop, target)):
        raise ShadowExecutionError("shadow prices are invalid")
    if direction == "BUY" and not stop < entry < target:
        raise ShadowExecutionError("BUY shadow price geometry is invalid")
    if direction == "SELL" and not target < entry < stop:
        raise ShadowExecutionError("SELL shadow price geometry is invalid")


def _positive_finite(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and isfinite(float(value))
        and float(value) > 0
    )


def _required_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ShadowExecutionError("shadow timestamp is invalid")
    return value.astimezone(UTC)
