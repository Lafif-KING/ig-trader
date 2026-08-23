"""Durable fenced PostgreSQL adapter for broker-neutral shadow state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from src.ig_trader.execution_lease import (
    ExecutionLeaseCoordinator,
    FencedCallbackRejected,
    FencedOperation,
)
from src.ig_trader.shadow_execution import (
    ShadowExecutionError,
    ShadowIntentRecord,
    ShadowLifecycle,
)

_READ = """
    SELECT ti.intent_id, ti.strategy_name, ti.epic, ti.lifecycle_state,
           ti.intent_payload, ti.created_at, ti.updated_at,
           sp.shadow_position_id, sp.strategy_id, sp.instrument, sp.direction,
           sp.entry_price, sp.stop_price, sp.target_price, sp.fencing_token,
           sp.opened_at, sp.closed_at, sp.status, sp.exit_price, sp.exit_reason,
           sp.created_at, sp.updated_at
    FROM trading.trade_intents ti
    LEFT JOIN trading.shadow_position_state sp ON sp.intent_id = ti.intent_id
    WHERE ti.intent_id = %s AND ti.execution_mode = 'SHADOW_DEMO'
"""

_ALLOWED_TRANSITIONS = {
    ShadowLifecycle.SHADOW_INTENT_CREATED: {ShadowLifecycle.OPEN},
    ShadowLifecycle.OPEN: {ShadowLifecycle.CLOSED, ShadowLifecycle.FAILED_SAFE},
    ShadowLifecycle.CLOSED: {ShadowLifecycle.RECONCILED},
}

_PRICE_PROJECTION_TOLERANCE = Decimal("1e-12")


class PostgresShadowStore:
    """Atomic ShadowStore implementation guarded by the lease fence transaction."""

    def __init__(
        self,
        lease: ExecutionLeaseCoordinator,
        connection_factory: Callable[[], Any],
    ) -> None:
        self._lease = lease
        self._connection_factory = connection_factory

    def get(self, intent_id: UUID) -> ShadowIntentRecord | None:
        try:
            with self._connection_factory() as connection:
                row = connection.execute(_READ, (intent_id,)).fetchone()
                connection.rollback()
            return None if row is None else _from_row(row)
        except ShadowExecutionError:
            raise
        except Exception:
            raise ShadowExecutionError("shadow persistence read failed closed") from None

    def active_position_count(self) -> int:
        try:
            with self._connection_factory() as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                read_only = connection.execute("SHOW transaction_read_only").fetchone()
                if read_only is None or str(read_only[0]).casefold() != "on":
                    raise ShadowExecutionError("shadow active-position read is not read-only")
                row = connection.execute(
                    """
                    SELECT count(*)
                    FROM trading.trade_intents ti
                    LEFT JOIN trading.shadow_position_state sp
                      ON sp.intent_id = ti.intent_id
                    WHERE ti.execution_mode = 'SHADOW_DEMO'
                      AND (
                        ti.lifecycle_state IN ('SHADOW_INTENT_CREATED', 'OPEN', 'FAILED_SAFE')
                        OR sp.status IN ('OPEN', 'FAILED_SAFE')
                      )
                    """
                ).fetchone()
                connection.rollback()
            if row is None or int(row[0]) < 0:
                raise ShadowExecutionError("shadow active-position count is ambiguous")
            return int(row[0])
        except ShadowExecutionError:
            raise
        except Exception:
            raise ShadowExecutionError("shadow active-position count failed closed") from None

    def put(self, record: ShadowIntentRecord) -> ShadowIntentRecord:
        if record.lifecycle is not ShadowLifecycle.SHADOW_INTENT_CREATED:
            raise ShadowExecutionError("only a new shadow intent can be persisted")
        if record.fencing_token != self._current_fencing_token():
            raise ShadowExecutionError("stale shadow fencing token")
        payload = _intent_payload(record)
        fingerprint = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()

        def write(cursor: Any) -> ShadowIntentRecord:
            cursor.execute(
                """
                SELECT ti.intent_id
                FROM trading.trade_intents ti
                LEFT JOIN trading.shadow_position_state sp ON sp.intent_id = ti.intent_id
                WHERE ti.execution_mode = 'SHADOW_DEMO'
                  AND (
                    ti.lifecycle_state IN ('SHADOW_INTENT_CREATED', 'OPEN', 'FAILED_SAFE')
                    OR sp.status IN ('OPEN', 'FAILED_SAFE')
                  )
                FOR UPDATE OF ti
                """
            )
            blocking_intent_ids = {UUID(str(row[0])) for row in cursor.fetchall()}
            if blocking_intent_ids:
                if blocking_intent_ids != {record.intent_id}:
                    raise ShadowExecutionError("an unresolved shadow position blocks a new intent")
                existing = _read_cursor(cursor, record.intent_id)
                if existing is None or _identity(existing) != _identity(record):
                    raise ShadowExecutionError("duplicate shadow intent conflicts")
                return existing
            cursor.execute(
                """
                INSERT INTO trading.trade_intents (
                    intent_id, idempotency_key, strategy_name, epic, execution_mode,
                    lifecycle_state, intent_payload, input_fingerprint_sha256,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'SHADOW_DEMO', %s, %s::jsonb, %s, %s, %s)
                ON CONFLICT (intent_id) DO NOTHING
                RETURNING intent_id
                """,
                (
                    record.intent_id,
                    f"shadow:{record.intent_id}",
                    record.strategy_id,
                    record.instrument,
                    record.lifecycle.value,
                    _canonical_json(payload),
                    fingerprint,
                    record.created_at,
                    record.updated_at,
                ),
            )
            inserted = cursor.fetchone() is not None
            result = _read_cursor(cursor, record.intent_id)
            if result is None:
                raise ShadowExecutionError("shadow intent persistence is ambiguous")
            if not inserted and _identity(result) != _identity(record):
                raise ShadowExecutionError("duplicate shadow intent conflicts")
            return result

        return self._run_fenced(FencedOperation.TRADE_INTENT, write)

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
        if to_state not in _ALLOWED_TRANSITIONS.get(from_state, set()):
            raise ShadowExecutionError("shadow lifecycle transition is invalid")
        if fencing_token != self._current_fencing_token():
            raise ShadowExecutionError("stale shadow fencing token")

        def write(cursor: Any) -> ShadowIntentRecord:
            if from_state is ShadowLifecycle.SHADOW_INTENT_CREATED:
                current = _read_cursor(cursor, intent_id)
                if current is None or current.lifecycle is not from_state:
                    raise ShadowExecutionError("shadow open transition conflicts")
                cursor.execute(
                    """
                    INSERT INTO trading.shadow_position_state (
                        shadow_position_id, intent_id, strategy_id, instrument, direction,
                        entry_price, stop_price, target_price, opened_at, status,
                        fencing_token, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s, %s)
                    """,
                    (
                        current.shadow_position_id,
                        current.intent_id,
                        current.strategy_id,
                        current.instrument,
                        current.direction,
                        current.entry_price,
                        current.stop_price,
                        current.target_price,
                        opened_at,
                        fencing_token,
                        current.created_at,
                        updated_at,
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE trading.shadow_position_state
                    SET status = %s, fencing_token = %s, updated_at = %s,
                        closed_at = COALESCE(%s, closed_at),
                        exit_price = COALESCE(%s, exit_price),
                        exit_reason = COALESCE(%s, exit_reason)
                    WHERE intent_id = %s AND status = %s
                    """,
                    (
                        to_state.value,
                        fencing_token,
                        updated_at,
                        closed_at,
                        exit_price,
                        exit_reason,
                        intent_id,
                        from_state.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ShadowExecutionError("shadow state transition conflicts")
            cursor.execute(
                """
                UPDATE trading.trade_intents
                SET lifecycle_state = %s, updated_at = %s
                WHERE intent_id = %s AND lifecycle_state = %s
                """,
                (to_state.value, updated_at, intent_id, from_state.value),
            )
            if cursor.rowcount != 1:
                raise ShadowExecutionError("shadow intent transition conflicts")
            result = _read_cursor(cursor, intent_id)
            if result is None or result.lifecycle is not to_state:
                raise ShadowExecutionError("shadow transition verification failed")
            return result

        return self._run_fenced(FencedOperation.RECONCILIATION, write)

    def _current_fencing_token(self) -> int:
        lease = self._lease.lease
        if not self._lease.authorized or lease is None:
            raise ShadowExecutionError("current shadow lease is unavailable")
        return lease.fencing_token

    def _run_fenced(self, operation: FencedOperation, callback: Callable[[Any], Any]) -> Any:
        def reject_domain_error(cursor: Any) -> Any:
            try:
                return callback(cursor)
            except ShadowExecutionError as error:
                raise FencedCallbackRejected(str(error)) from None

        try:
            return self._lease.run_state_change(operation, reject_domain_error)
        except FencedCallbackRejected as error:
            raise ShadowExecutionError(str(error)) from None
        except ShadowExecutionError:
            raise
        except Exception:
            raise ShadowExecutionError("shadow persistence transaction failed closed") from None


def _read_cursor(cursor: Any, intent_id: UUID) -> ShadowIntentRecord | None:
    cursor.execute(_READ, (intent_id,))
    row = cursor.fetchone()
    return None if row is None else _from_row(row)


def _from_row(row: tuple[object, ...]) -> ShadowIntentRecord:
    payload = row[4]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ShadowExecutionError("shadow intent payload is invalid")
    payload_entry_price = _payload_price(payload, "entry_price")
    payload_stop_price = _payload_price(payload, "stop_price")
    payload_target_price = _payload_price(payload, "target_price")
    intent_lifecycle = _lifecycle(row[3])
    has_position = row[7] is not None
    if not has_position and intent_lifecycle is not ShadowLifecycle.SHADOW_INTENT_CREATED:
        raise ShadowExecutionError("shadow lifecycle requires durable position state")
    position_lifecycle = _lifecycle(row[17]) if has_position else None
    if has_position and intent_lifecycle is not position_lifecycle:
        raise ShadowExecutionError("shadow lifecycle tables disagree")
    _require_payload_value(payload, "strategy_id", row[1])
    _require_payload_value(payload, "instrument", row[2])
    if has_position:
        _require_payload_value(payload, "shadow_position_id", row[7])
        _require_payload_value(payload, "strategy_id", row[8])
        _require_payload_value(payload, "instrument", row[9])
        _require_payload_value(payload, "direction", row[10])
        _require_payload_value(payload, "entry_price", row[11])
        _require_payload_value(payload, "stop_price", row[12])
        _require_payload_value(payload, "target_price", row[13])
    lifecycle = position_lifecycle or intent_lifecycle
    return ShadowIntentRecord(
        shadow_position_id=UUID(str(row[7] or payload["shadow_position_id"])),
        intent_id=UUID(str(row[0])),
        strategy_id=str(row[1]),
        instrument=str(row[2]),
        direction=str(row[10] or payload["direction"]),
        # Immutable intent economics remain authoritative after the typed
        # projection has been strictly checked for representation-equivalence.
        entry_price=payload_entry_price,
        stop_price=payload_stop_price,
        target_price=payload_target_price,
        fencing_token=(
            _fencing_token(row[14]) if has_position else _fencing_token(payload["fencing_token"])
        ),
        created_at=row[20] or row[5],
        updated_at=row[21] or row[6],
        lifecycle=lifecycle,
        opened_at=row[15],
        closed_at=row[16],
        exit_price=float(row[18]) if row[18] is not None else None,
        exit_reason=str(row[19]) if row[19] is not None else None,
    )


def _intent_payload(record: ShadowIntentRecord) -> dict[str, object]:
    return {
        "direction": record.direction,
        "entry_price": record.entry_price,
        "fencing_token": record.fencing_token,
        "instrument": record.instrument,
        "shadow_position_id": str(record.shadow_position_id),
        "stop_price": record.stop_price,
        "strategy_id": record.strategy_id,
        "target_price": record.target_price,
    }


def _identity(record: ShadowIntentRecord) -> tuple[object, ...]:
    return (
        record.shadow_position_id,
        record.intent_id,
        record.strategy_id,
        record.instrument,
        record.direction,
        record.entry_price,
        record.stop_price,
        record.target_price,
    )


def _lifecycle(value: object) -> ShadowLifecycle:
    try:
        return ShadowLifecycle(str(value))
    except ValueError:
        raise ShadowExecutionError("shadow lifecycle state is invalid") from None


def _require_payload_value(payload: dict[str, object], field: str, expected: object) -> None:
    if field not in payload:
        raise ShadowExecutionError("shadow intent payload is incomplete")
    actual = payload[field]
    if field in {"entry_price", "stop_price", "target_price"}:
        matches = _price_projection_matches(actual, expected)
    else:
        matches = str(actual) == str(expected)
    if not matches:
        raise ShadowExecutionError("shadow intent payload and table columns disagree")


def _payload_price(payload: dict[str, object], field: str) -> float:
    if field not in payload:
        raise ShadowExecutionError("shadow intent payload is incomplete")
    decimal_value = _positive_decimal(payload[field])
    return float(decimal_value)


def _price_projection_matches(payload_value: object, projection_value: object) -> bool:
    try:
        return abs(_positive_decimal(payload_value) - _positive_decimal(projection_value)) <= (
            _PRICE_PROJECTION_TOLERANCE
        )
    except ShadowExecutionError:
        return False


def _positive_decimal(value: object) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ShadowExecutionError("shadow price projection is invalid")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ShadowExecutionError("shadow price projection is invalid") from None
    if not decimal_value.is_finite() or decimal_value <= 0:
        raise ShadowExecutionError("shadow price projection is invalid")
    return decimal_value


def _fencing_token(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ShadowExecutionError("shadow fencing token is invalid")
    return value


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
