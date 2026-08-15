"""Persistent TradeIntent state machine and append-only evidence journal."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.ig_trader.offline_paper.domain import (
    ExecutionMode,
    LifecycleEvent,
    LifecycleState,
    RiskDecision,
    Side,
    TradeIntent,
)


class StateStoreError(RuntimeError):
    """Raised when durable local state cannot be trusted."""


_TRANSITIONS = {
    LifecycleState.SIGNAL_DETECTED: {
        LifecycleState.INTENT_CREATED,
        LifecycleState.RISK_REJECTED,
        LifecycleState.FAILED_SAFE,
    },
    LifecycleState.INTENT_CREATED: {
        LifecycleState.ORDER_SUBMITTED,
        LifecycleState.FAILED_SAFE,
    },
    LifecycleState.ORDER_SUBMITTED: {
        LifecycleState.ORDER_ACCEPTED,
        LifecycleState.ORDER_REJECTED,
        LifecycleState.FAILED_SAFE,
    },
    LifecycleState.ORDER_ACCEPTED: {
        LifecycleState.POSITION_OPEN,
        LifecycleState.FAILED_SAFE,
    },
    LifecycleState.ORDER_REJECTED: {
        LifecycleState.RECONCILED,
        LifecycleState.FAILED_SAFE,
    },
    LifecycleState.POSITION_OPEN: {
        LifecycleState.EXIT_REQUESTED,
        LifecycleState.FAILED_SAFE,
    },
    LifecycleState.EXIT_REQUESTED: {
        LifecycleState.POSITION_CLOSED,
        LifecycleState.FAILED_SAFE,
    },
    LifecycleState.POSITION_CLOSED: {
        LifecycleState.RECONCILED,
        LifecycleState.FAILED_SAFE,
    },
    LifecycleState.RISK_REJECTED: set(),
    LifecycleState.RECONCILED: set(),
    LifecycleState.FAILED_SAFE: set(),
}


class TradeIntentStore:
    """SQLite authority for intent identity, state, and evidence lineage."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS offline_runs (
                        cycle_id TEXT PRIMARY KEY,
                        input_fingerprint TEXT NOT NULL,
                        configuration_hash TEXT NOT NULL,
                        execution_mode TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS trade_intents (
                        intent_id TEXT PRIMARY KEY,
                        cycle_id TEXT NOT NULL UNIQUE,
                        candidate_id TEXT NOT NULL UNIQUE,
                        epic TEXT NOT NULL,
                        lifecycle_state TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS lifecycle_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        intent_id TEXT NOT NULL,
                        from_state TEXT,
                        to_state TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        FOREIGN KEY(intent_id) REFERENCES trade_intents(intent_id)
                    );
                    CREATE TABLE IF NOT EXISTS risk_decisions (
                        cycle_id TEXT NOT NULL,
                        candidate_id TEXT NOT NULL,
                        state TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        PRIMARY KEY(cycle_id, candidate_id)
                    );
                    CREATE TABLE IF NOT EXISTS evidence_lineage (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        cycle_id TEXT NOT NULL,
                        identity TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        UNIQUE(cycle_id, identity, phase)
                    );
                    CREATE TABLE IF NOT EXISTS exit_requests (
                        intent_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(intent_id) REFERENCES trade_intents(intent_id)
                    );
                    """
                )
        except (OSError, sqlite3.Error) as error:
            raise StateStoreError("offline state initialization failed") from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def initialize_run(
        self,
        *,
        cycle_id: str,
        input_fingerprint: str,
        configuration_hash: str,
        occurred_at: datetime,
    ) -> bool:
        if not all(_text(item) for item in (cycle_id, input_fingerprint, configuration_hash)):
            return False
        if not _aware(occurred_at):
            return False
        timestamp = occurred_at.astimezone(UTC).isoformat()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM offline_runs WHERE cycle_id=?", (cycle_id,)
                ).fetchone()
                if row:
                    return (
                        row["input_fingerprint"] == input_fingerprint
                        and row["configuration_hash"] == configuration_hash
                        and row["execution_mode"] == ExecutionMode.OFFLINE_PAPER.value
                    )
                connection.execute(
                    "INSERT INTO offline_runs VALUES (?,?,?,?,?)",
                    (
                        cycle_id,
                        input_fingerprint,
                        configuration_hash,
                        ExecutionMode.OFFLINE_PAPER.value,
                        timestamp,
                    ),
                )
            return True
        except sqlite3.Error:
            return False

    def create_intent(self, intent: TradeIntent) -> bool:
        if intent.lifecycle_state is not LifecycleState.SIGNAL_DETECTED:
            return False
        payload = _encode(_intent_document(intent))
        timestamp = intent.created_at.astimezone(UTC).isoformat()
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT payload FROM trade_intents WHERE intent_id=?",
                    (intent.intent_id,),
                ).fetchone()
                if existing:
                    return existing["payload"] == payload
                connection.execute(
                    "INSERT INTO trade_intents VALUES (?,?,?,?,?,?,?,?)",
                    (
                        intent.intent_id,
                        intent.cycle_id,
                        intent.candidate_id,
                        intent.epic,
                        intent.lifecycle_state.value,
                        payload,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    "INSERT INTO lifecycle_events"
                    "(intent_id,from_state,to_state,reason,occurred_at) "
                    "VALUES (?,NULL,?,?,?)",
                    (
                        intent.intent_id,
                        LifecycleState.SIGNAL_DETECTED.value,
                        "EXACT_SCALPER_SIGNAL_SELECTED",
                        timestamp,
                    ),
                )
            return True
        except (sqlite3.Error, TypeError, ValueError):
            return False

    def transition(
        self,
        intent_id: str,
        target: LifecycleState,
        *,
        reason: str,
        occurred_at: datetime,
    ) -> bool:
        if not _text(intent_id) or not _text(reason) or not _aware(occurred_at):
            return False
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT lifecycle_state,payload FROM trade_intents WHERE intent_id=?",
                    (intent_id,),
                ).fetchone()
                if not row:
                    return False
                current = LifecycleState(row["lifecycle_state"])
                if target not in _TRANSITIONS[current]:
                    return False
                document = json.loads(row["payload"])
                document["lifecycle_state"] = target.value
                encoded = _encode(document)
                timestamp = occurred_at.astimezone(UTC).isoformat()
                cursor = connection.execute(
                    "UPDATE trade_intents SET lifecycle_state=?,payload=?,updated_at=? "
                    "WHERE intent_id=? AND lifecycle_state=?",
                    (target.value, encoded, timestamp, intent_id, current.value),
                )
                if cursor.rowcount != 1:
                    return False
                connection.execute(
                    "INSERT INTO lifecycle_events"
                    "(intent_id,from_state,to_state,reason,occurred_at) VALUES (?,?,?,?,?)",
                    (intent_id, current.value, target.value, reason, timestamp),
                )
            return True
        except (json.JSONDecodeError, sqlite3.Error, KeyError, ValueError, TypeError):
            return False

    def record_exit_request(
        self,
        intent_id: str,
        payload: dict[str, Any],
        *,
        occurred_at: datetime,
    ) -> bool:
        if not _text(intent_id) or not _aware(occurred_at):
            return False
        encoded = _encode(payload)
        timestamp = occurred_at.astimezone(UTC).isoformat()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM exit_requests WHERE intent_id=?", (intent_id,)
                ).fetchone()
                if row:
                    return row["payload"] == encoded
                connection.execute(
                    "INSERT INTO exit_requests VALUES (?,?,?)",
                    (intent_id, encoded, timestamp),
                )
            return True
        except (sqlite3.Error, TypeError, ValueError):
            return False

    def exit_request(self, intent_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM exit_requests WHERE intent_id=?", (intent_id,)
                ).fetchone()
            return json.loads(row["payload"]) if row else None
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            return None

    def record_risk_decision(
        self,
        *,
        cycle_id: str,
        candidate_id: str,
        decision: RiskDecision,
        occurred_at: datetime,
    ) -> bool:
        state = LifecycleState.SIGNAL_DETECTED if decision.allowed else LifecycleState.RISK_REJECTED
        payload = _encode(_risk_document(decision))
        timestamp = occurred_at.astimezone(UTC).isoformat()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT state,payload FROM risk_decisions WHERE cycle_id=? AND candidate_id=?",
                    (cycle_id, candidate_id),
                ).fetchone()
                if row:
                    return row["state"] == state.value and row["payload"] == payload
                connection.execute(
                    "INSERT INTO risk_decisions VALUES (?,?,?,?,?)",
                    (cycle_id, candidate_id, state.value, payload, timestamp),
                )
            return True
        except (sqlite3.Error, TypeError, ValueError):
            return False

    def append_lineage(
        self,
        *,
        cycle_id: str,
        identity: str,
        phase: str,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> bool:
        if not all(_text(item) for item in (cycle_id, identity, phase)) or not _aware(occurred_at):
            return False
        encoded = _encode(payload)
        timestamp = occurred_at.astimezone(UTC).isoformat()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM evidence_lineage "
                    "WHERE cycle_id=? AND identity=? AND phase=?",
                    (cycle_id, identity, phase),
                ).fetchone()
                if row:
                    return row["payload"] == encoded
                connection.execute(
                    "INSERT INTO evidence_lineage"
                    "(cycle_id,identity,phase,payload,occurred_at) VALUES (?,?,?,?,?)",
                    (cycle_id, identity, phase, encoded, timestamp),
                )
            return True
        except (sqlite3.Error, TypeError, ValueError):
            return False

    def intents(self) -> tuple[TradeIntent, ...] | None:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT payload FROM trade_intents ORDER BY created_at,intent_id"
                ).fetchall()
            return tuple(_intent_from_document(json.loads(row["payload"])) for row in rows)
        except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def get(self, intent_id: str) -> TradeIntent | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM trade_intents WHERE intent_id=?", (intent_id,)
                ).fetchone()
            return _intent_from_document(json.loads(row["payload"])) if row else None
        except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def events(self, intent_id: str) -> tuple[LifecycleEvent, ...] | None:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM lifecycle_events WHERE intent_id=? ORDER BY sequence",
                    (intent_id,),
                ).fetchall()
            return tuple(
                LifecycleEvent(
                    row["sequence"],
                    row["intent_id"],
                    LifecycleState(row["from_state"]) if row["from_state"] else None,
                    LifecycleState(row["to_state"]),
                    row["reason"],
                    _datetime(row["occurred_at"]),
                )
                for row in rows
            )
        except (sqlite3.Error, ValueError, TypeError):
            return None

    def lineage(self, cycle_id: str) -> tuple[dict[str, Any], ...] | None:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM evidence_lineage WHERE cycle_id=? ORDER BY sequence",
                    (cycle_id,),
                ).fetchall()
            return tuple(
                {
                    "sequence": row["sequence"],
                    "identity": row["identity"],
                    "phase": row["phase"],
                    "payload": json.loads(row["payload"]),
                    "occurred_at": row["occurred_at"],
                }
                for row in rows
            )
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            return None


def _intent_document(intent: TradeIntent) -> dict[str, Any]:
    value = asdict(intent)
    value["created_at"] = intent.created_at.astimezone(UTC).isoformat()
    value["side"] = intent.side.value
    value["risk_decision"] = _risk_document(intent.risk_decision)
    value["source_candle_references"] = list(intent.source_candle_references)
    value["execution_mode"] = intent.execution_mode.value
    value["lifecycle_state"] = intent.lifecycle_state.value
    return value


def _intent_from_document(value: dict[str, Any]) -> TradeIntent:
    return TradeIntent(
        intent_id=value["intent_id"],
        created_at=_datetime(value["created_at"]),
        cycle_id=value["cycle_id"],
        candidate_id=value["candidate_id"],
        epic=value["epic"],
        side=Side(value["side"]),
        strategy=value["strategy"],
        strategy_version=value["strategy_version"],
        signal_inputs=dict(value["signal_inputs"]),
        confidence=float(value["confidence"]),
        spread_pips=float(value["spread_pips"]),
        risk_decision=_risk_from_document(value["risk_decision"]),
        size=float(value["size"]),
        stop_level=float(value["stop_level"]),
        target_level=float(value["target_level"]),
        source_candle_references=tuple(value["source_candle_references"]),
        source_fingerprint=value["source_fingerprint"],
        execution_mode=ExecutionMode(value["execution_mode"]),
        lifecycle_state=LifecycleState(value["lifecycle_state"]),
    )


def _risk_document(decision: RiskDecision) -> dict[str, Any]:
    return asdict(decision)


def _risk_from_document(value: dict[str, Any]) -> RiskDecision:
    return RiskDecision(**value)


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone aware")
    return parsed.astimezone(UTC)


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()
