"""Restart-safe local SQLite persistence for DQ-02 Demo execution records.

The database contains immutable request economics and lifecycle evidence only.
It deliberately has no credentials, authentication headers, or raw broker
response storage.
"""

# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from src.ig_trader.demo_execution import (
    DemoConfirmation,
    DemoConfirmationStatus,
    DemoDirection,
    DemoExecutionError,
    DemoExecutionLifecycle,
    DemoExecutionRecord,
    DemoExecutionRequest,
    DemoOrderType,
    DemoPosition,
    DemoRiskApproval,
    DemoSubmission,
)


class SQLiteDemoExecutionStore:
    """A local single-host store with request/deal-reference uniqueness.

    SQLite transactions make duplicate starts and process restarts deterministic:
    a matching intent returns its original record, while a conflicting intent or
    deal reference fails closed.
    """

    _INCOMPLETE = (
        DemoExecutionLifecycle.SUBMITTED.value,
        DemoExecutionLifecycle.AMBIGUOUS.value,
        DemoExecutionLifecycle.CONFIRMED_ACCEPTED.value,
        DemoExecutionLifecycle.CLOSE_REQUESTED.value,
    )

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def get(self, intent_id: UUID) -> DemoExecutionRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT record_json FROM demo_execution_records WHERE intent_id = ?",
                (str(intent_id),),
            ).fetchone()
        return _decode_record(row[0]) if row is not None else None

    def put(self, record: DemoExecutionRecord) -> DemoExecutionRecord:
        encoded = _encode_record(record)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json FROM demo_execution_records WHERE intent_id = ?",
                (str(record.request.intent_id),),
            ).fetchone()
            if row is not None:
                existing = _decode_record(row[0])
                if existing.request != record.request:
                    raise DemoExecutionError("duplicate Demo intent conflicts")
                return existing
            try:
                connection.execute(
                    """
                    INSERT INTO demo_execution_records (
                        intent_id, global_cycle_id, deal_reference, lifecycle,
                        record_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(record.request.intent_id),
                        str(record.request.global_cycle_id),
                        record.request.deal_reference,
                        record.lifecycle.value,
                        encoded,
                        _timestamp(record.request.created_at),
                        _timestamp(datetime.now(UTC)),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise DemoExecutionError("duplicate Demo deal reference conflicts") from error
        return record

    def replace(self, record: DemoExecutionRecord) -> DemoExecutionRecord:
        encoded = _encode_record(record)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json FROM demo_execution_records WHERE intent_id = ?",
                (str(record.request.intent_id),),
            ).fetchone()
            if row is None:
                raise DemoExecutionError("Demo execution record is unknown or conflicting")
            existing = _decode_record(row[0])
            if existing.request != record.request:
                raise DemoExecutionError("Demo execution record is unknown or conflicting")
            connection.execute(
                """
                UPDATE demo_execution_records
                SET lifecycle = ?, record_json = ?, updated_at = ?
                WHERE intent_id = ?
                """,
                (
                    record.lifecycle.value,
                    encoded,
                    _timestamp(datetime.now(UTC)),
                    str(record.request.intent_id),
                ),
            )
        return record

    def has_other_cycle_record(self, global_cycle_id: UUID, intent_id: UUID) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM demo_execution_records
                WHERE global_cycle_id = ? AND intent_id != ? LIMIT 1
                """,
                (str(global_cycle_id), str(intent_id)),
            ).fetchone()
        return row is not None

    def incomplete_records(self) -> tuple[DemoExecutionRecord, ...]:
        """Return only restart-sensitive lifecycles that require broker reconciliation."""

        placeholders = ", ".join("?" for _ in self._INCOMPLETE)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT record_json FROM demo_execution_records
                WHERE lifecycle IN ({placeholders})
                ORDER BY created_at ASC
                """,
                self._INCOMPLETE,
            ).fetchall()
        return tuple(_decode_record(row[0]) for row in rows)

    def all_records(self) -> tuple[DemoExecutionRecord, ...]:
        """Return local execution evidence for exact broker-position ownership checks."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT record_json FROM demo_execution_records ORDER BY created_at ASC"
            ).fetchall()
        return tuple(_decode_record(row[0]) for row in rows)

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS demo_execution_records (
                    intent_id TEXT PRIMARY KEY NOT NULL,
                    global_cycle_id TEXT NOT NULL,
                    deal_reference TEXT UNIQUE NOT NULL,
                    lifecycle TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_demo_execution_cycle
                ON demo_execution_records(global_cycle_id)
                """
            )

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _encode_record(record: DemoExecutionRecord) -> str:
    request = record.request
    document = {
        "request": {
            "intent_id": str(request.intent_id),
            "global_cycle_id": str(request.global_cycle_id),
            "epic": request.epic,
            "direction": request.direction.value,
            "size": str(request.size),
            "currency_code": request.currency_code,
            "expiry": request.expiry,
            "order_type": request.order_type.value,
            "force_open": request.force_open,
            "guaranteed_stop": request.guaranteed_stop,
            "stop_distance": _decimal_text(request.stop_distance),
            "stop_level": _decimal_text(request.stop_level),
            "limit_distance": _decimal_text(request.limit_distance),
            "limit_level": _decimal_text(request.limit_level),
            "deal_reference": request.deal_reference,
            "configuration_identity": request.configuration_identity,
            "risk_approval": {
                "configuration_identity": request.risk_approval.configuration_identity,
                "allowed": request.risk_approval.allowed,
                "evaluated_at": _timestamp(request.risk_approval.evaluated_at),
            },
            "fencing_token": request.fencing_token,
            "created_at": _timestamp(request.created_at),
        },
        "lifecycle": record.lifecycle.value,
        "submission": _submission_document(record.submission),
        "confirmation": _confirmation_document(record.confirmation),
        "position": _position_document(record.position),
        "close_submission": _submission_document(record.close_submission),
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _decode_record(encoded: str) -> DemoExecutionRecord:
    try:
        document = json.loads(encoded)
        request_document = _document(document, "request")
        risk_document = _document(request_document, "risk_approval")
        request = DemoExecutionRequest(
            intent_id=UUID(_required_text(request_document, "intent_id")),
            global_cycle_id=UUID(_required_text(request_document, "global_cycle_id")),
            epic=_required_text(request_document, "epic"),
            direction=DemoDirection(_required_text(request_document, "direction")),
            size=Decimal(_required_text(request_document, "size")),
            currency_code=_required_text(request_document, "currency_code"),
            expiry=_required_text(request_document, "expiry"),
            order_type=DemoOrderType(_required_text(request_document, "order_type")),
            force_open=_required_bool(request_document, "force_open"),
            guaranteed_stop=_required_bool(request_document, "guaranteed_stop"),
            stop_distance=_optional_decimal(request_document.get("stop_distance")),
            stop_level=_optional_decimal(request_document.get("stop_level")),
            limit_distance=_optional_decimal(request_document.get("limit_distance")),
            limit_level=_optional_decimal(request_document.get("limit_level")),
            deal_reference=_required_text(request_document, "deal_reference"),
            configuration_identity=_required_text(request_document, "configuration_identity"),
            risk_approval=DemoRiskApproval(
                configuration_identity=_required_text(risk_document, "configuration_identity"),
                allowed=_required_bool(risk_document, "allowed"),
                evaluated_at=_parse_timestamp(_required_text(risk_document, "evaluated_at")),
            ),
            fencing_token=_required_int(request_document, "fencing_token"),
            created_at=_parse_timestamp(_required_text(request_document, "created_at")),
        )
        return DemoExecutionRecord(
            request=request,
            lifecycle=DemoExecutionLifecycle(_required_text(document, "lifecycle")),
            submission=_decode_submission(document.get("submission")),
            confirmation=_decode_confirmation(document.get("confirmation")),
            position=_decode_position(document.get("position")),
            close_submission=_decode_submission(document.get("close_submission")),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise DemoExecutionError("durable Demo execution record is invalid") from error


def _submission_document(value: DemoSubmission | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"deal_reference": value.deal_reference, "deal_id": value.deal_id}


def _confirmation_document(value: DemoConfirmation | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "deal_reference": value.deal_reference,
        "deal_id": value.deal_id,
        "deal_status": value.deal_status.value,
        "status": value.status,
        "epic": value.epic,
        "direction": value.direction.value if value.direction else None,
        "size": _decimal_text(value.size),
        "level": _decimal_text(value.level),
        "stop_level": _decimal_text(value.stop_level),
        "limit_level": _decimal_text(value.limit_level),
    }


def _position_document(value: DemoPosition | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "deal_id": value.deal_id,
        "epic": value.epic,
        "direction": value.direction.value,
        "size": str(value.size),
    }


def _decode_submission(value: object) -> DemoSubmission | None:
    if value is None:
        return None
    document = _document_value(value)
    return DemoSubmission(
        deal_reference=_required_text(document, "deal_reference"),
        deal_id=_optional_text(document.get("deal_id")),
    )


def _decode_confirmation(value: object) -> DemoConfirmation | None:
    if value is None:
        return None
    document = _document_value(value)
    direction = _optional_text(document.get("direction"))
    return DemoConfirmation(
        deal_reference=_required_text(document, "deal_reference"),
        deal_id=_optional_text(document.get("deal_id")),
        deal_status=DemoConfirmationStatus(_required_text(document, "deal_status")),
        status=_required_text(document, "status"),
        epic=_optional_text(document.get("epic")),
        direction=DemoDirection(direction) if direction else None,
        size=_optional_decimal(document.get("size")),
        level=_optional_decimal(document.get("level")),
        stop_level=_optional_decimal(document.get("stop_level")),
        limit_level=_optional_decimal(document.get("limit_level")),
    )


def _decode_position(value: object) -> DemoPosition | None:
    if value is None:
        return None
    document = _document_value(value)
    return DemoPosition(
        deal_id=_required_text(document, "deal_id"),
        epic=_required_text(document, "epic"),
        direction=DemoDirection(_required_text(document, "direction")),
        size=Decimal(_required_text(document, "size")),
    )


def _document(document: object, name: str) -> dict[str, object]:
    if not isinstance(document, dict):
        raise ValueError("record document is invalid")
    return _document_value(document.get(name))


def _document_value(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("record document is invalid")
    return value


def _required_text(document: dict[str, object], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError("record text is missing")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _required_bool(document: dict[str, object], name: str) -> bool:
    value = document.get(name)
    if not isinstance(value, bool):
        raise ValueError("record boolean is missing")
    return value


def _required_int(document: dict[str, object], name: str) -> int:
    value = document.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("record integer is missing")
    return value


def _optional_decimal(value: object) -> Decimal | None:
    return Decimal(value) if isinstance(value, str) else None


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DemoExecutionError("Demo timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("record timestamp is invalid")
    return parsed.astimezone(UTC)
