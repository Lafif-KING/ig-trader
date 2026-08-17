"""Broker-neutral durable SHADOW_DEMO position persistence.

The caller supplies a current :class:`ExecutionLeaseCoordinator`; all writes are
performed through its existing PostgreSQL fence.  This module intentionally has
no broker identifier fields and no broker-client imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from typing import Any
from uuid import UUID

from src.ig_trader.execution_lease import ExecutionLeaseCoordinator, FencedOperation


class ShadowPositionState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    RECONCILED = "RECONCILED"
    FAILED_SAFE = "FAILED_SAFE"


class ShadowPositionStateError(RuntimeError):
    """Shadow state is incomplete, conflicting, or unavailable."""


@dataclass(frozen=True)
class ShadowPosition:
    shadow_position_id: UUID
    intent_id: UUID
    strategy_id: str
    instrument: str
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    opened_at: datetime
    fencing_token: int
    created_at: datetime
    updated_at: datetime
    status: ShadowPositionState = ShadowPositionState.OPEN
    closed_at: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None


class PostgresShadowPositionStore:
    """Fenced PostgreSQL persistence for one hypothetical position per intent."""

    def __init__(self, lease: ExecutionLeaseCoordinator) -> None:
        self._lease = lease

    def create(self, position: ShadowPosition) -> ShadowPosition:
        _validate(position)
        self._require_current_fence(position.fencing_token)

        def write(cursor: Any) -> ShadowPosition:
            cursor.execute(
                """
                INSERT INTO trading.shadow_position_state (
                    shadow_position_id, intent_id, strategy_id, instrument, direction,
                    entry_price, stop_price, target_price, opened_at, status,
                    fencing_token, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (intent_id) DO NOTHING
                RETURNING shadow_position_id, intent_id, strategy_id, instrument, direction,
                    entry_price, stop_price, target_price, opened_at, closed_at, status,
                    exit_price, exit_reason, fencing_token, created_at, updated_at
                """,
                _write_values(position),
            )
            row = cursor.fetchone()
            if row is not None:
                return _from_row(row)
            cursor.execute(
                """
                SELECT shadow_position_id, intent_id, strategy_id, instrument, direction,
                    entry_price, stop_price, target_price, opened_at, closed_at, status,
                    exit_price, exit_reason, fencing_token, created_at, updated_at
                FROM trading.shadow_position_state WHERE intent_id = %s
                """,
                (position.intent_id,),
            )
            existing = cursor.fetchone()
            if existing is None:
                raise ShadowPositionStateError("SHADOW_POSITION_PERSISTENCE_AMBIGUOUS")
            result = _from_row(existing)
            if result != position:
                raise ShadowPositionStateError("SHADOW_POSITION_INTENT_DEDUP_CONFLICT")
            return result

        return self._lease.run_state_change(FencedOperation.TRADE_INTENT, write)

    def transition(
        self,
        *,
        intent_id: UUID,
        from_state: ShadowPositionState,
        to_state: ShadowPositionState,
        fencing_token: int,
        updated_at: datetime,
        closed_at: datetime | None = None,
        exit_price: float | None = None,
        exit_reason: str | None = None,
    ) -> ShadowPosition:
        if not isinstance(fencing_token, int) or fencing_token <= 0 or _utc(updated_at) is None:
            raise ShadowPositionStateError("SHADOW_POSITION_TRANSITION_INVALID")
        self._require_current_fence(fencing_token)

        def write(cursor: Any) -> ShadowPosition:
            cursor.execute(
                """
                UPDATE trading.shadow_position_state
                SET status = %s, fencing_token = %s, updated_at = %s,
                    closed_at = %s, exit_price = %s, exit_reason = %s
                WHERE intent_id = %s AND status = %s
                RETURNING shadow_position_id, intent_id, strategy_id, instrument, direction,
                    entry_price, stop_price, target_price, opened_at, closed_at, status,
                    exit_price, exit_reason, fencing_token, created_at, updated_at
                """,
                (to_state.value, fencing_token, updated_at, closed_at, exit_price, exit_reason,
                 intent_id, from_state.value),
            )
            row = cursor.fetchone()
            if row is None:
                raise ShadowPositionStateError("SHADOW_POSITION_TRANSITION_CONFLICT_OR_UNKNOWN")
            return _from_row(row)

        return self._lease.run_state_change(FencedOperation.RECONCILIATION, write)

    def _require_current_fence(self, fencing_token: int) -> None:
        lease = self._lease.lease
        if not self._lease.authorized or lease is None or fencing_token != lease.fencing_token:
            raise ShadowPositionStateError("SHADOW_POSITION_STALE_FENCING_TOKEN")


def _write_values(value: ShadowPosition) -> tuple[object, ...]:
    return (value.shadow_position_id, value.intent_id, value.strategy_id, value.instrument,
            value.direction, value.entry_price, value.stop_price, value.target_price,
            value.opened_at, value.status.value, value.fencing_token, value.created_at,
            value.updated_at)


def _from_row(row: tuple[object, ...]) -> ShadowPosition:
    return ShadowPosition(
        UUID(str(row[0])), UUID(str(row[1])), str(row[2]), str(row[3]), str(row[4]),
        float(row[5]), float(row[6]), float(row[7]), _required_utc(row[8]), int(row[13]),
        _required_utc(row[14]), _required_utc(row[15]), ShadowPositionState(str(row[10])),
        _utc(row[9]), float(row[11]) if row[11] is not None else None,
        str(row[12]) if row[12] is not None else None,
    )


def _validate(value: ShadowPosition) -> None:
    if (not isinstance(value, ShadowPosition) or not value.strategy_id.strip()
            or not value.instrument.strip() or value.direction not in {"BUY", "SELL"}
            or value.status is not ShadowPositionState.OPEN or value.closed_at is not None
            or value.exit_price is not None or value.exit_reason is not None
            or not isinstance(value.fencing_token, int) or value.fencing_token <= 0):
        raise ShadowPositionStateError("SHADOW_POSITION_INVALID")
    for item in (value.entry_price, value.stop_price, value.target_price):
        if (
            isinstance(item, bool)
            or not isinstance(item, int | float)
            or not isfinite(float(item))
            or item <= 0
        ):
            raise ShadowPositionStateError("SHADOW_POSITION_PRICE_INVALID")
    for timestamp in (value.opened_at, value.created_at, value.updated_at):
        _required_utc(timestamp)


def _required_utc(value: object) -> datetime:
    result = _utc(value)
    if result is None:
        raise ShadowPositionStateError("SHADOW_POSITION_TIMESTAMP_INVALID")
    return result


def _utc(value: object) -> datetime | None:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC)
    return None
