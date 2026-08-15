"""Deterministic SQLite PaperBroker implementing the offline execution ports."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from src.ig_trader.offline_paper.domain import (
    AccountSnapshot,
    BrokerOrder,
    Exit,
    Fill,
    Position,
    ReconciliationSnapshot,
    Side,
)


class PaperBroker:
    """Local virtual broker; it has no IG, HTTP, stream, or credential dependency."""

    def __init__(
        self,
        path: str | Path,
        *,
        account_id: str,
        currency: str,
        starting_balance: float,
        rejected_epics: frozenset[str] = frozenset(),
    ) -> None:
        if not _text(account_id) or not _currency(currency) or not _positive(starting_balance):
            raise ValueError("paper account configuration is invalid")
        if not isinstance(rejected_epics, frozenset) or any(
            not _text(item) for item in rejected_epics
        ):
            raise ValueError("paper rejection policy is invalid")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.account_id = account_id
        self.currency = currency
        self.starting_balance = float(starting_balance)
        self.rejected_epics = rejected_epics
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS paper_accounts (
                        account_id TEXT PRIMARY KEY,
                        currency TEXT NOT NULL,
                        starting_balance REAL NOT NULL,
                        balance REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS paper_orders (
                        order_reference TEXT PRIMARY KEY,
                        intent_id TEXT NOT NULL UNIQUE,
                        payload TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS paper_fills (
                        fill_reference TEXT PRIMARY KEY,
                        intent_id TEXT NOT NULL UNIQUE,
                        order_reference TEXT NOT NULL UNIQUE,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(order_reference) REFERENCES paper_orders(order_reference)
                    );
                    CREATE TABLE IF NOT EXISTS paper_positions (
                        position_reference TEXT PRIMARY KEY,
                        intent_id TEXT NOT NULL UNIQUE,
                        epic TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        status TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_open_epic
                    ON paper_positions(epic) WHERE status='OPEN';
                    CREATE TABLE IF NOT EXISTS paper_exits (
                        exit_reference TEXT PRIMARY KEY,
                        intent_id TEXT NOT NULL UNIQUE,
                        position_reference TEXT NOT NULL UNIQUE,
                        payload TEXT NOT NULL,
                        FOREIGN KEY(position_reference)
                            REFERENCES paper_positions(position_reference)
                    );
                    """
                )
                row = connection.execute(
                    "SELECT * FROM paper_accounts WHERE account_id=?", (self.account_id,)
                ).fetchone()
                if row:
                    if (
                        row["currency"] != self.currency
                        or row["starting_balance"] != self.starting_balance
                        or not _positive(row["balance"])
                    ):
                        raise ValueError("paper account state conflicts with configuration")
                else:
                    connection.execute(
                        "INSERT INTO paper_accounts VALUES (?,?,?,?)",
                        (
                            self.account_id,
                            self.currency,
                            self.starting_balance,
                            self.starting_balance,
                        ),
                    )
        except (OSError, sqlite3.Error) as error:
            raise ValueError("paper broker state unavailable") from error

    def submit(self, order: BrokerOrder) -> Fill:
        """Accept or reject once; exact duplicate submissions are idempotent."""

        if not _valid_order(order):
            return _rejection(order, "INVALID_ORDER")
        encoded_order = _encode(_order_document(order))
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT payload FROM paper_orders WHERE intent_id=?", (order.intent_id,)
                ).fetchone()
                if existing:
                    if existing["payload"] != encoded_order:
                        return _rejection(order, "IDEMPOTENCY_CONFLICT")
                    fill = self.fill_for_intent(order.intent_id)
                    return fill or _rejection(order, "FILL_STATE_UNKNOWN")
                status = "REJECTED" if order.epic in self.rejected_epics else "ACCEPTED"
                connection.execute(
                    "INSERT INTO paper_orders VALUES (?,?,?,?,?)",
                    (
                        order.order_reference,
                        order.intent_id,
                        encoded_order,
                        status,
                        order.submitted_at.astimezone(UTC).isoformat(),
                    ),
                )
                if status == "REJECTED":
                    fill = _rejection(order, "DETERMINISTIC_POLICY_REJECTION")
                else:
                    open_count = connection.execute(
                        "SELECT COUNT(*) AS count FROM paper_positions WHERE status='OPEN'"
                    ).fetchone()["count"]
                    if open_count != 0:
                        raise sqlite3.IntegrityError("paper position limit reached")
                    position_reference = _reference("position", order.intent_id)
                    fill = Fill(
                        _reference("fill", order.intent_id),
                        order.order_reference,
                        position_reference,
                        True,
                        "DETERMINISTIC_MARKET_FILL",
                        order.requested_price,
                        order.size,
                        order.submitted_at.astimezone(UTC),
                    )
                    position = Position(
                        position_reference,
                        order.intent_id,
                        order.epic,
                        order.side,
                        order.size,
                        order.requested_price,
                        order.stop_level,
                        order.target_level,
                        order.pip_size,
                        order.pip_value_account_currency,
                        order.submitted_at.astimezone(UTC),
                    )
                    connection.execute(
                        "INSERT INTO paper_positions VALUES (?,?,?,?,'OPEN')",
                        (
                            position.position_reference,
                            position.intent_id,
                            position.epic,
                            _encode(_position_document(position)),
                        ),
                    )
                connection.execute(
                    "INSERT INTO paper_fills VALUES (?,?,?,?)",
                    (
                        fill.fill_reference,
                        order.intent_id,
                        order.order_reference,
                        _encode(_fill_document(fill)),
                    ),
                )
            return fill
        except (sqlite3.Error, TypeError, ValueError):
            return _rejection(order, "PAPER_STATE_UNKNOWN")

    def close(self, request: Exit) -> Exit | None:
        """Close one verified open paper position with an idempotent reference."""

        if not _valid_exit_request(request):
            return None
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT payload FROM paper_exits WHERE intent_id=?", (request.intent_id,)
                ).fetchone()
                if existing:
                    stored = _exit_from_document(json.loads(existing["payload"]))
                    return stored if _same_exit_request(stored, request) else None
                row = connection.execute(
                    "SELECT payload FROM paper_positions "
                    "WHERE intent_id=? AND position_reference=? AND status='OPEN'",
                    (request.intent_id, request.position_reference),
                ).fetchone()
                if not row:
                    return None
                position = _position_from_document(json.loads(row["payload"]))
                sign = 1.0 if position.side is Side.BUY else -1.0
                profit_loss = (
                    (request.price - position.entry_price)
                    / position.pip_size
                    * position.pip_value_account_currency
                    * position.size
                    * sign
                )
                if not isfinite(profit_loss):
                    return None
                completed = Exit(
                    request.exit_reference,
                    request.position_reference,
                    request.intent_id,
                    request.price,
                    request.reason,
                    profit_loss,
                    request.closed_at.astimezone(UTC),
                )
                connection.execute(
                    "UPDATE paper_positions SET status='CLOSED' "
                    "WHERE position_reference=? AND status='OPEN'",
                    (position.position_reference,),
                )
                connection.execute(
                    "UPDATE paper_accounts SET balance=balance+? WHERE account_id=?",
                    (profit_loss, self.account_id),
                )
                connection.execute(
                    "INSERT INTO paper_exits VALUES (?,?,?,?)",
                    (
                        completed.exit_reference,
                        completed.intent_id,
                        completed.position_reference,
                        _encode(_exit_document(completed)),
                    ),
                )
            return completed
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
            return None

    def account_snapshot(self, *, as_of: datetime) -> AccountSnapshot | None:
        if not _aware(as_of):
            return None
        try:
            with self._connect() as connection:
                account = connection.execute(
                    "SELECT * FROM paper_accounts WHERE account_id=?", (self.account_id,)
                ).fetchone()
                rows = connection.execute(
                    "SELECT payload FROM paper_positions "
                    "WHERE status='OPEN' ORDER BY position_reference"
                ).fetchall()
            if not account or not _positive(account["balance"]):
                return None
            return AccountSnapshot(
                self.account_id,
                account["currency"],
                float(account["balance"]),
                float(account["starting_balance"]),
                tuple(_position_from_document(json.loads(row["payload"])) for row in rows),
                as_of.astimezone(UTC),
                True,
            )
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
            return None

    def order_for_intent(self, intent_id: str) -> BrokerOrder | None:
        return self._object("paper_orders", intent_id, _order_from_document)

    def fill_for_intent(self, intent_id: str) -> Fill | None:
        return self._object("paper_fills", intent_id, _fill_from_document)

    def position_for_intent(self, intent_id: str) -> Position | None:
        return self._object("paper_positions", intent_id, _position_from_document)

    def exit_for_intent(self, intent_id: str) -> Exit | None:
        return self._object("paper_exits", intent_id, _exit_from_document)

    def reconciliation_snapshot(self, *, as_of: datetime) -> ReconciliationSnapshot | None:
        account = self.account_snapshot(as_of=as_of)
        if account is None:
            return None
        try:
            with self._connect() as connection:
                order_rows = connection.execute(
                    "SELECT payload FROM paper_orders ORDER BY order_reference"
                ).fetchall()
                fill_rows = connection.execute(
                    "SELECT payload FROM paper_fills ORDER BY fill_reference"
                ).fetchall()
                exit_rows = connection.execute(
                    "SELECT payload FROM paper_exits ORDER BY exit_reference"
                ).fetchall()
            return ReconciliationSnapshot(
                account,
                tuple(_order_from_document(json.loads(row["payload"])) for row in order_rows),
                tuple(_fill_from_document(json.loads(row["payload"])) for row in fill_rows),
                tuple(_exit_from_document(json.loads(row["payload"])) for row in exit_rows),
            )
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
            return None

    def _object(self, table: str, intent_id: str, loader: object) -> object | None:
        if not _text(intent_id) or table not in {
            "paper_orders",
            "paper_fills",
            "paper_positions",
            "paper_exits",
        }:
            return None
        try:
            with self._connect() as connection:
                row = connection.execute(
                    f"SELECT payload FROM {table} WHERE intent_id=?",  # noqa: S608
                    (intent_id,),
                ).fetchone()
            return loader(json.loads(row["payload"])) if row else None
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
            return None


def _reference(kind: str, intent_id: str) -> str:
    return f"PAPER-{kind.upper()}-{uuid5(NAMESPACE_URL, f'{kind}:{intent_id}').hex}"


def _rejection(order: BrokerOrder, reason: str) -> Fill:
    timestamp = (
        order.submitted_at.astimezone(UTC)
        if isinstance(order, BrokerOrder) and _aware(order.submitted_at)
        else datetime.now(UTC)
    )
    intent_id = order.intent_id if isinstance(order, BrokerOrder) else "INVALID"
    reference = order.order_reference if isinstance(order, BrokerOrder) else "INVALID"
    return Fill(
        _reference("fill", intent_id),
        reference,
        None,
        False,
        reason,
        None,
        None,
        timestamp,
    )


def _valid_order(order: object) -> bool:
    if not isinstance(order, BrokerOrder):
        return False
    if not all(_text(item) for item in (order.order_reference, order.intent_id, order.epic)):
        return False
    if not isinstance(order.side, Side) or not _aware(order.submitted_at):
        return False
    if any(
        not _positive(item)
        for item in (
            order.size,
            order.requested_price,
            order.stop_level,
            order.target_level,
            order.pip_size,
            order.pip_value_account_currency,
        )
    ):
        return False
    if order.side is Side.BUY:
        return order.stop_level < order.requested_price < order.target_level
    return order.target_level < order.requested_price < order.stop_level


def _valid_exit_request(value: object) -> bool:
    return bool(
        isinstance(value, Exit)
        and all(
            _text(item)
            for item in (
                value.exit_reference,
                value.position_reference,
                value.intent_id,
                value.reason,
            )
        )
        and _positive(value.price)
        and _aware(value.closed_at)
        and isfinite(value.profit_loss)
    )


def _same_exit_request(stored: Exit, request: Exit) -> bool:
    return (
        stored.exit_reference == request.exit_reference
        and stored.position_reference == request.position_reference
        and stored.intent_id == request.intent_id
        and stored.price == request.price
        and stored.reason == request.reason
        and stored.closed_at == request.closed_at.astimezone(UTC)
    )


def _order_document(order: BrokerOrder) -> dict[str, object]:
    value = asdict(order)
    value["side"] = order.side.value
    value["submitted_at"] = order.submitted_at.astimezone(UTC).isoformat()
    return value


def _order_from_document(value: dict[str, object]) -> BrokerOrder:
    return BrokerOrder(
        str(value["order_reference"]),
        str(value["intent_id"]),
        str(value["epic"]),
        Side(str(value["side"])),
        float(value["size"]),
        float(value["requested_price"]),
        float(value["stop_level"]),
        float(value["target_level"]),
        float(value["pip_size"]),
        float(value["pip_value_account_currency"]),
        _datetime(value["submitted_at"]),
    )


def _fill_document(fill: Fill) -> dict[str, object]:
    value = asdict(fill)
    value["timestamp"] = fill.timestamp.astimezone(UTC).isoformat()
    return value


def _fill_from_document(value: dict[str, object]) -> Fill:
    return Fill(
        str(value["fill_reference"]),
        str(value["order_reference"]),
        str(value["position_reference"]) if value["position_reference"] else None,
        bool(value["accepted"]),
        str(value["reason"]),
        float(value["price"]) if value["price"] is not None else None,
        float(value["size"]) if value["size"] is not None else None,
        _datetime(value["timestamp"]),
    )


def _position_document(position: Position) -> dict[str, object]:
    value = asdict(position)
    value["side"] = position.side.value
    value["opened_at"] = position.opened_at.astimezone(UTC).isoformat()
    return value


def _position_from_document(value: dict[str, object]) -> Position:
    return Position(
        str(value["position_reference"]),
        str(value["intent_id"]),
        str(value["epic"]),
        Side(str(value["side"])),
        float(value["size"]),
        float(value["entry_price"]),
        float(value["stop_level"]),
        float(value["target_level"]),
        float(value["pip_size"]),
        float(value["pip_value_account_currency"]),
        _datetime(value["opened_at"]),
    )


def _exit_document(value: Exit) -> dict[str, object]:
    document = asdict(value)
    document["closed_at"] = value.closed_at.astimezone(UTC).isoformat()
    return document


def _exit_from_document(value: dict[str, object]) -> Exit:
    return Exit(
        str(value["exit_reference"]),
        str(value["position_reference"]),
        str(value["intent_id"]),
        float(value["price"]),
        str(value["reason"]),
        float(value["profit_loss"]),
        _datetime(value["closed_at"]),
    )


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp is unaware")
    return parsed.astimezone(UTC)


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _currency(value: object) -> bool:
    return isinstance(value, str) and len(value) == 3 and value.isalpha() and value.isupper()


def _positive(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and isfinite(float(value))
        and float(value) > 0
    )
