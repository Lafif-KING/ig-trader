"""Durable fenced PostgreSQL adapter for broker-neutral shadow state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from src.ig_trader.execution_lease import (
    ExecutionLeaseCoordinator,
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
           sp.shadow_position_id, sp.direction, sp.entry_price, sp.stop_price,
           sp.target_price, sp.fencing_token, sp.opened_at, sp.closed_at,
           sp.status, sp.exit_price, sp.exit_reason, sp.created_at, sp.updated_at
    FROM trading.trade_intents ti
    LEFT JOIN trading.shadow_position_state sp ON sp.intent_id = ti.intent_id
    WHERE ti.intent_id = %s AND ti.execution_mode = 'SHADOW_DEMO'
"""


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
                        ti.lifecycle_state IN ('SHADOW_INTENT_CREATED', 'OPEN')
                        OR sp.status = 'OPEN'
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
        try:
            return self._lease.run_state_change(operation, callback)
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
    lifecycle = ShadowLifecycle(str(row[15] if row[7] is not None else row[3]))
    return ShadowIntentRecord(
        shadow_position_id=UUID(str(row[7] or payload["shadow_position_id"])),
        intent_id=UUID(str(row[0])),
        strategy_id=str(row[1]),
        instrument=str(row[2]),
        direction=str(row[8] or payload["direction"]),
        entry_price=float(row[9] or payload["entry_price"]),
        stop_price=float(row[10] or payload["stop_price"]),
        target_price=float(row[11] or payload["target_price"]),
        fencing_token=int(row[12] or payload["fencing_token"]),
        created_at=row[18] or row[5],
        updated_at=row[19] or row[6],
        lifecycle=lifecycle,
        opened_at=row[13],
        closed_at=row[14],
        exit_price=float(row[16]) if row[16] is not None else None,
        exit_reason=str(row[17]) if row[17] is not None else None,
    )


def _intent_payload(record: ShadowIntentRecord) -> dict[str, object]:
    return {
        "direction": record.direction,
        "entry_price": record.entry_price,
        "fencing_token": record.fencing_token,
        "shadow_position_id": str(record.shadow_position_id),
        "stop_price": record.stop_price,
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
        record.fencing_token,
    )


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
