"""Local-only lifecycle controller for the DQ-02 IG Demo qualification worker.

The controller never gives the dashboard a broker client.  The Streamlit page
may start this local process only after an explicit click; this process then
performs its own endpoint, identity, reconciliation, singleton and kill-switch
checks before creating a worker.  The initial execution registry is empty, so
the worker is a broker-monitoring/reconciliation robot until a separately
reviewed registration exists.
"""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from src.ig_trader.config import settings
from src.ig_trader.demo_execution import (
    DEMO_ENVIRONMENT,
    DemoAuthorityGate,
    DemoExecutionCore,
    DemoExecutionLifecycle,
    DemoExecutionMode,
    KillSwitchState,
)
from src.ig_trader.demo_operator_models import (
    InstrumentDiscoveryStatus,
    approved_demo_execution_registry,
    classify_discovery,
    research_assignments,
)
from src.ig_trader.demo_store import SQLiteDemoExecutionStore
from src.ig_trader.demo_transport import (
    IG_DEMO_BASE_URL,
    IGDemoAccount,
    IGDemoRESTTransport,
    validate_ig_demo_endpoint,
)
from src.ig_trader.session import SessionManager
from src.ig_trader.strategy_lab.models import INITIAL_INSTRUMENT_REGISTRY

DEFAULT_RUNTIME_DIRECTORY = Path(".runtime") / "demo_operator"
DEFAULT_STORE_PATH = DEFAULT_RUNTIME_DIRECTORY / "demo_execution.sqlite"
DEFAULT_SNAPSHOT_PATH = DEFAULT_RUNTIME_DIRECTORY / "operator_snapshot.json"
HEARTBEAT_MAX_AGE = timedelta(seconds=20)
WORKER_POLL_SECONDS = 3.0


class DemoOperatorError(RuntimeError):
    """A local Demo operator action was rejected before a broker mutation."""


class WorkerLauncher(Protocol):
    def __call__(self, store_path: Path) -> int: ...


@dataclass(frozen=True)
class LocalDemoOperatorConfig:
    base_url: str
    expected_demo_account_id: str | None
    control_enabled: bool
    hosted: bool
    credentials_available: bool


@dataclass(frozen=True)
class WorkerState:
    state: str
    pid: int | None
    heartbeat_at: datetime | None
    kill_switch_state: KillSwitchState
    started_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class DemoOperatorSnapshot:
    environment: str = "IG_DEMO"
    rest_status: str = "DISCONNECTED"
    streaming_status: str = "DISCONNECTED"
    robot_state: str = "STOPPED"
    account: str | None = None
    balance: str | None = None
    available_funds: str | None = None
    total_open_positions: int = 0
    total_open_pnl: str | None = None
    today_realized_pnl: str | None = None
    last_successful_sync: str | None = None
    last_confirmation: str | None = None
    kill_switch_state: str = KillSwitchState.BLOCKING.value
    execution_authority: str = "OFF"
    approved_demo_epic_count: int = 0
    approved_demo_strategy_count: int = 0
    risk_configuration_status: str = "UNKNOWN"
    reconciliation_status: str = "UNKNOWN"
    working_orders: int | None = None
    last_critical_error: str | None = None
    message: str = "No local Demo worker has written a snapshot."
    positions: tuple[dict[str, object], ...] = ()
    alerts: tuple[str, ...] = ()


def local_demo_operator_config(environ: Mapping[str, str] | None = None) -> LocalDemoOperatorConfig:
    """Read the explicit local-only gate without ever echoing credential values."""

    source = os.environ if environ is None else environ
    fallback = environ is None
    local = source.get("DEMO_OPERATOR_LOCAL", "").casefold() == "true" or (
        fallback and settings.demo_operator_local
    )
    hosted = source.get("DASHBOARD_HOSTED", "").casefold() == "true"
    base_url = source.get("IG_BASE_URL") or (settings.ig_base_url if fallback else IG_DEMO_BASE_URL)
    expected = source.get("IG_EXPECTED_DEMO_ACCOUNT_ID") or (
        settings.ig_expected_demo_account_id if fallback else None
    )
    credentials_available = all(
        bool(source.get(name, "") or (getattr(settings, name.casefold(), "") if fallback else ""))
        for name in ("IG_API_KEY", "IG_IDENTIFIER", "IG_PASSWORD")
    )
    return LocalDemoOperatorConfig(
        base_url=base_url,
        expected_demo_account_id=expected.strip() if expected and expected.strip() else None,
        control_enabled=local and not hosted,
        hosted=hosted,
        credentials_available=credentials_available,
    )


class SQLiteWorkerRegistry:
    """PID + heartbeat + durable singleton state in the local Demo store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def current(self) -> WorkerState:
        with closing(self._connection()) as connection, connection:
            row = connection.execute(
                "SELECT state, pid, heartbeat_at, kill_switch_state, started_at, last_error "
                "FROM demo_operator_control WHERE singleton_id = 1"
            ).fetchone()
        if row is None:
            return WorkerState("STOPPED", None, None, KillSwitchState.BLOCKING, None, None)
        return WorkerState(
            state=str(row["state"]),
            pid=int(row["pid"]) if row["pid"] is not None else None,
            heartbeat_at=_parse_timestamp(row["heartbeat_at"]),
            kill_switch_state=KillSwitchState(str(row["kill_switch_state"])),
            started_at=_parse_timestamp(row["started_at"]),
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        )

    def acquire_start(self, owner_pid: int) -> None:
        """Atomically reserve the singleton before a child worker is created."""

        now = datetime.now(UTC)
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._current_in_transaction(connection)
            if existing.kill_switch_state is KillSwitchState.BLOCKING:
                raise DemoOperatorError("EMERGENCY KILL is blocking Demo robot start")
            if existing.state in {"STARTING", "RUNNING", "PAUSED", "STOP_REQUESTED"}:
                if existing.pid is not None and _pid_alive(existing.pid):
                    if (
                        existing.heartbeat_at is None
                        or now - existing.heartbeat_at <= HEARTBEAT_MAX_AGE
                    ):
                        raise DemoOperatorError("a Demo robot worker is already running")
                    self._write(
                        connection, "SAFE_STOP", existing.pid, now, now, "worker heartbeat is stale"
                    )
                    raise DemoOperatorError(
                        "existing Demo robot heartbeat is stale; safe stop required"
                    )
                if (
                    existing.heartbeat_at is not None
                    and now - existing.heartbeat_at <= HEARTBEAT_MAX_AGE
                ):
                    raise DemoOperatorError("existing Demo robot state cannot be proven stale")
            self._write(connection, "STARTING", owner_pid, now, now, None)

    def set_worker_pid(self, worker_pid: int) -> None:
        if worker_pid <= 0:
            raise DemoOperatorError("worker process identity is invalid")
        now = datetime.now(UTC)
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._current_in_transaction(connection)
            if state.state != "STARTING" or state.kill_switch_state is KillSwitchState.BLOCKING:
                raise DemoOperatorError("Demo worker cannot be assigned after safe stop")
            self._write(connection, "RUNNING", worker_pid, now, state.started_at or now, None)

    def heartbeat(self, worker_pid: int) -> WorkerState:
        now = datetime.now(UTC)
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._current_in_transaction(connection)
            if state.pid != worker_pid or state.state not in {
                "RUNNING",
                "PAUSED",
                "STOP_REQUESTED",
            }:
                raise DemoOperatorError("Demo worker ownership cannot be proven")
            self._write(
                connection,
                state.state,
                worker_pid,
                now,
                state.started_at or now,
                state.last_error,
            )
        return self.current()

    def request_stop(self) -> None:
        now = datetime.now(UTC)
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._current_in_transaction(connection)
            if state.state in {"RUNNING", "PAUSED", "STARTING"}:
                self._write(
                    connection,
                    "STOP_REQUESTED",
                    state.pid,
                    now,
                    state.started_at or now,
                    state.last_error,
                )

    def pause_new_entries(self) -> None:
        """Pause entry evaluation while the existing worker keeps reconciling."""

        now = datetime.now(UTC)
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._current_in_transaction(connection)
            if state.kill_switch_state is KillSwitchState.BLOCKING:
                raise DemoOperatorError("EMERGENCY KILL blocks resume and entry state changes")
            if state.state != "RUNNING":
                raise DemoOperatorError("Demo robot is not running and cannot be paused")
            self._write(
                connection,
                "PAUSED",
                state.pid,
                now,
                state.started_at or now,
                "New entries paused; reconciliation remains active.",
            )

    def resume_new_entries(self) -> None:
        """Resume a paused worker only; this never releases Emergency Kill."""

        now = datetime.now(UTC)
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._current_in_transaction(connection)
            if state.kill_switch_state is KillSwitchState.BLOCKING:
                raise DemoOperatorError("EMERGENCY KILL must be released outside the dashboard")
            if state.state != "PAUSED":
                raise DemoOperatorError("Demo robot is not paused and cannot be resumed")
            if state.pid is None or not _pid_alive(state.pid):
                raise DemoOperatorError("paused Demo worker process cannot be proven alive")
            if state.heartbeat_at is None or now - state.heartbeat_at > HEARTBEAT_MAX_AGE:
                raise DemoOperatorError("paused Demo worker heartbeat is stale")
            self._write(
                connection,
                "RUNNING",
                state.pid,
                now,
                state.started_at or now,
                None,
            )

    def emergency_kill(self) -> None:
        now = datetime.now(UTC)
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._current_in_transaction(connection)
            self._write(
                connection,
                "SAFE_STOP",
                state.pid,
                now,
                state.started_at or now,
                "Emergency Kill activated; new orders and modifications are blocked.",
                kill_switch_state=KillSwitchState.BLOCKING,
            )

    def release_kill_for_local_recovery(self) -> None:
        """Requires an explicit local CLI action; no dashboard control releases a kill."""

        now = datetime.now(UTC)
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._current_in_transaction(connection)
            if state.pid is not None and _pid_alive(state.pid):
                raise DemoOperatorError("cannot release kill while a worker process exists")
            self._write(
                connection,
                "STOPPED",
                None,
                now,
                state.started_at or now,
                None,
                kill_switch_state=KillSwitchState.RELEASED,
            )

    def stopped(self, worker_pid: int, error: str | None = None) -> None:
        now = datetime.now(UTC)
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._current_in_transaction(connection)
            if state.pid != worker_pid:
                return
            final_state = "SAFE_STOP" if error else "STOPPED"
            self._write(
                connection,
                final_state,
                None,
                now,
                state.started_at or now,
                error,
                kill_switch_state=state.kill_switch_state,
            )

    def _initialize(self) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS demo_operator_control (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    state TEXT NOT NULL,
                    pid INTEGER,
                    heartbeat_at TEXT,
                    kill_switch_state TEXT NOT NULL,
                    started_at TEXT,
                    last_error TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO demo_operator_control (
                    singleton_id, state, pid, heartbeat_at, kill_switch_state, started_at, last_error
                ) VALUES (1, 'STOPPED', NULL, NULL, 'RELEASED', NULL, NULL)
                """
            )

    def _current_in_transaction(self, connection: sqlite3.Connection) -> WorkerState:
        row = connection.execute(
            "SELECT state, pid, heartbeat_at, kill_switch_state, started_at, last_error "
            "FROM demo_operator_control WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise DemoOperatorError("Demo worker lock is unavailable")
        return WorkerState(
            str(row["state"]),
            int(row["pid"]) if row["pid"] is not None else None,
            _parse_timestamp(row["heartbeat_at"]),
            KillSwitchState(str(row["kill_switch_state"])),
            _parse_timestamp(row["started_at"]),
            str(row["last_error"]) if row["last_error"] is not None else None,
        )

    @staticmethod
    def _write(
        connection: sqlite3.Connection,
        state: str,
        pid: int | None,
        heartbeat_at: datetime,
        started_at: datetime,
        last_error: str | None,
        *,
        kill_switch_state: KillSwitchState | None = None,
    ) -> None:
        current = connection.execute(
            "SELECT kill_switch_state FROM demo_operator_control WHERE singleton_id = 1"
        ).fetchone()
        value = kill_switch_state or KillSwitchState(str(current["kill_switch_state"]))
        connection.execute(
            """
            UPDATE demo_operator_control
            SET state = ?, pid = ?, heartbeat_at = ?, kill_switch_state = ?, started_at = ?, last_error = ?
            WHERE singleton_id = 1
            """,
            (state, pid, _timestamp(heartbeat_at), value.value, _timestamp(started_at), last_error),
        )

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        return connection


class DemoRobotController:
    """Separate-process controller; the initial worker is monitor-only by design."""

    def __init__(
        self,
        *,
        config: LocalDemoOperatorConfig,
        store: SQLiteDemoExecutionStore,
        transport_factory: Callable[[], IGDemoRESTTransport],
        worker_launcher: WorkerLauncher,
        snapshot_path: Path = DEFAULT_SNAPSHOT_PATH,
    ) -> None:
        self.config = config
        self.store = store
        self.transport_factory = transport_factory
        self.worker_launcher = worker_launcher
        self.snapshot_path = snapshot_path
        self.registry = SQLiteWorkerRegistry(store.path)

    @classmethod
    def from_environment(
        cls,
        *,
        store_path: Path = DEFAULT_STORE_PATH,
        snapshot_path: Path = DEFAULT_SNAPSHOT_PATH,
    ) -> DemoRobotController:
        config = local_demo_operator_config()

        def transport_factory() -> IGDemoRESTTransport:
            validate_ig_demo_endpoint(config.base_url)
            return IGDemoRESTTransport(session=SessionManager(), base_url=config.base_url)

        return cls(
            config=config,
            store=SQLiteDemoExecutionStore(store_path),
            transport_factory=transport_factory,
            worker_launcher=_launch_worker,
            snapshot_path=snapshot_path,
        )

    def preflight(self) -> DemoOperatorSnapshot:
        """Perform allowed read-only identity, position, and registry checks."""

        self._validate_start_configuration()
        transport = self.transport_factory()
        account = transport.get_account()
        self._verify_account(account)
        positions = transport.list_position_details()
        registrations = approved_demo_execution_registry()
        documents, reconciliation_status = self._position_documents(positions)
        snapshot = DemoOperatorSnapshot(
            rest_status="CONNECTED",
            streaming_status="DISCONNECTED",
            robot_state=self.registry.current().state,
            account=_safe_account_label(account.account_id),
            balance=_text_decimal(account.balance),
            available_funds=_text_decimal(account.available_funds),
            total_open_positions=len(positions),
            total_open_pnl=_text_decimal(account.profit_loss),
            last_successful_sync=_timestamp(datetime.now(UTC)),
            kill_switch_state=self.registry.current().kill_switch_state.value,
            message=(
                "Demo endpoint and expected account identity verified by a read-only preflight."
            ),
            approved_demo_epic_count=len({item.epic for item in registrations}),
            approved_demo_strategy_count=len(registrations),
            reconciliation_status=reconciliation_status,
            positions=documents,
            alerts=(
                "Streaming is not connected; new entries remain blocked until a single controlled "
                "streaming session is proven current.",
                "No instrument is in the Demo execution registry; the worker will reconcile only.",
            ),
        )
        self.write_snapshot(snapshot)
        return snapshot

    def start(self) -> WorkerState:
        """Validate first, reconcile first, then launch exactly one local worker."""

        snapshot = self.preflight()
        transport = self.transport_factory()
        for record in self.store.incomplete_records():
            execution = DemoExecutionCore(transport=transport, store=self.store)
            if record.lifecycle is DemoExecutionLifecycle.CLOSE_REQUESTED:
                execution.reconcile_close(record.request.intent_id)
            else:
                execution.reconcile_open(record.request.intent_id)
        self.registry.acquire_start(os.getpid())
        try:
            worker_pid = self.worker_launcher(self.store.path)
            self.registry.set_worker_pid(worker_pid)
        except Exception as error:
            self.registry.stopped(os.getpid(), "worker launch failed")
            raise DemoOperatorError("Demo worker did not start") from error
        state = self.registry.current()
        self.write_snapshot(
            DemoOperatorSnapshot(
                **{
                    **asdict(snapshot),
                    "robot_state": state.state,
                    "message": "Demo worker started.",
                }
            )
        )
        return state

    def stop(self) -> WorkerState:
        """Request graceful shutdown; reconciliation monitoring ends only at worker exit."""

        self.registry.request_stop()
        state = self.registry.current()
        self.write_snapshot(
            DemoOperatorSnapshot(
                robot_state=state.state,
                kill_switch_state=state.kill_switch_state.value,
                message="Graceful stop requested; the worker will reconcile before exit.",
            )
        )
        return state

    def pause(self) -> WorkerState:
        """Block new entries but leave safe position management running."""

        self.registry.pause_new_entries()
        state = self.registry.current()
        self.write_snapshot(
            DemoOperatorSnapshot(
                robot_state=state.state,
                kill_switch_state=state.kill_switch_state.value,
                message="New entries paused; reconciliation and safe position management continue.",
            )
        )
        return state

    def resume(self) -> WorkerState:
        """Resume an existing paused worker; Emergency Kill remains separate."""

        self.registry.resume_new_entries()
        state = self.registry.current()
        self.write_snapshot(
            DemoOperatorSnapshot(
                robot_state=state.state,
                kill_switch_state=state.kill_switch_state.value,
                message="Pause released. Every execution gate is still required for new entries.",
            )
        )
        return state

    def emergency_kill(self) -> WorkerState:
        """Block all new orders and modifications without automatically flattening positions."""

        self.registry.emergency_kill()
        state = self.registry.current()
        self.write_snapshot(
            DemoOperatorSnapshot(
                robot_state=state.state,
                kill_switch_state=state.kill_switch_state.value,
                message="Emergency Kill active. Existing positions are not automatically closed.",
            )
        )
        return state

    def close_all_demo_positions(self) -> int:
        """Flatten only exact locally owned positions through the DQ-01 core.

        Unknown broker ownership blocks every close before the first mutation.
        Each close uses the reconciled deal ID, confirmation, and absence check;
        the sequence halts rather than retrying an ambiguous broker response.
        """

        snapshot = self.preflight()
        transport = self.transport_factory()
        positions = transport.list_positions()
        records = {
            record.position.deal_id: record
            for record in self.store.all_records()
            if record.lifecycle is DemoExecutionLifecycle.OPEN_RECONCILED
            and record.position is not None
        }
        if any(position.deal_id not in records for position in positions):
            raise DemoOperatorError("broker position ownership is unknown; flattening is blocked")
        execution = DemoExecutionCore(transport=transport, store=self.store)
        for position in positions:
            record = records[position.deal_id]
            requested = execution.request_close(
                record.request.intent_id, self._close_authority(record, len(positions))
            )
            reconciled = execution.reconcile_close(record.request.intent_id)
            if (
                requested.lifecycle is not DemoExecutionLifecycle.CLOSE_REQUESTED
                or reconciled.lifecycle is not DemoExecutionLifecycle.CLOSED_RECONCILED
            ):
                raise DemoOperatorError("close result is ambiguous; further closes are blocked")
        self.write_snapshot(
            DemoOperatorSnapshot(
                **{
                    **asdict(snapshot),
                    "total_open_positions": 0,
                    "message": "All locally owned Demo positions were closed and reconciled.",
                }
            )
        )
        return len(positions)

    def worker_once(self, worker_pid: int) -> WorkerState:
        """One safe worker cycle: sync/reconcile, never create an entry without a permit."""

        state = self.registry.heartbeat(worker_pid)
        if state.kill_switch_state is KillSwitchState.BLOCKING or state.state == "STOP_REQUESTED":
            self.registry.stopped(worker_pid)
            return self.registry.current()
        transport = self.transport_factory()
        positions = transport.list_position_details()
        execution = DemoExecutionCore(transport=transport, store=self.store)
        for record in self.store.incomplete_records():
            if record.lifecycle is DemoExecutionLifecycle.CLOSE_REQUESTED:
                execution.reconcile_close(record.request.intent_id)
            else:
                execution.reconcile_open(record.request.intent_id)
        registrations = approved_demo_execution_registry()
        if registrations:
            raise DemoOperatorError(
                "a Demo execution registry needs a reviewed worker strategy adapter"
            )
        documents, reconciliation_status = self._position_documents(positions)
        self.write_snapshot(
            DemoOperatorSnapshot(
                rest_status="CONNECTED",
                streaming_status="DISCONNECTED",
                robot_state=state.state,
                total_open_positions=len(positions),
                last_successful_sync=_timestamp(datetime.now(UTC)),
                kill_switch_state=state.kill_switch_state.value,
                message=(
                    "New entries paused; monitoring and reconciliation remain active."
                    if state.state == "PAUSED"
                    else (
                        "Monitoring and reconciliation active; no qualified execution "
                        "registration exists."
                    )
                ),
                approved_demo_epic_count=len({item.epic for item in registrations}),
                approved_demo_strategy_count=len(registrations),
                reconciliation_status=reconciliation_status,
                positions=documents,
                alerts=("STALE PRICE FEED: new entries are blocked.",),
            )
        )
        return self.registry.current()

    def worker_loop(self) -> None:
        worker_pid = os.getpid()
        try:
            while True:
                state = self.worker_once(worker_pid)
                if state.state not in {"RUNNING", "PAUSED"}:
                    return
                time.sleep(WORKER_POLL_SECONDS)
        except Exception:
            self.registry.stopped(
                worker_pid, "worker safe-stopped after a reconciliation or network error"
            )
            raise

    def write_snapshot(self, snapshot: DemoOperatorSnapshot) -> None:
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.snapshot_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(snapshot), sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.snapshot_path)

    def _position_documents(
        self, positions: tuple[object, ...]
    ) -> tuple[tuple[dict[str, object], ...], str]:
        records = {
            record.position.deal_id: record
            for record in self.store.all_records()
            if record.lifecycle is DemoExecutionLifecycle.OPEN_RECONCILED
            and record.position is not None
        }
        documents = tuple(
            _position_document(position, records.get(position.deal_id)) for position in positions
        )
        reconciled = all(item.get("ownership") == "RECONCILED" for item in documents)
        status = "NORMAL" if reconciled else "BLOCKED"
        return documents, status

    def _validate_start_configuration(self) -> None:
        if not self.config.control_enabled or self.config.hosted:
            raise DemoOperatorError("Demo Operator controls are local-only and disabled")
        validate_ig_demo_endpoint(self.config.base_url)
        if not self.config.credentials_available:
            raise DemoOperatorError("Demo credentials are unavailable")
        if not self.config.expected_demo_account_id:
            raise DemoOperatorError("expected Demo account ID is unavailable")
        if self.registry.current().kill_switch_state is KillSwitchState.BLOCKING:
            raise DemoOperatorError("EMERGENCY KILL is blocking Demo robot start")

    def _verify_account(self, account: IGDemoAccount) -> None:
        if account.account_id != self.config.expected_demo_account_id:
            raise DemoOperatorError(
                "authenticated Demo account does not match the configured expected account"
            )

    def _close_authority(self, record: object, position_count: int) -> DemoAuthorityGate:
        request = getattr(record, "request", None)
        if request is None:
            raise DemoOperatorError("local execution ownership is invalid")
        state = self.registry.current()
        if state.kill_switch_state is KillSwitchState.BLOCKING:
            raise DemoOperatorError("EMERGENCY KILL blocks Demo position changes")
        return DemoAuthorityGate(
            execution_mode=DemoExecutionMode.DEMO_EXECUTION,
            demo_order_authority=True,
            environment=DEMO_ENVIRONMENT,
            expected_demo_account_id=self.config.expected_demo_account_id,
            authenticated_account_id=self.config.expected_demo_account_id,
            lease_valid=state.state in {"RUNNING", "STOP_REQUESTED"},
            current_fencing_token=request.fencing_token,
            global_position_count=position_count,
            global_position_limit=max(position_count, 1),
            approved_epics=frozenset({request.epic}),
            kill_switch_state=state.kill_switch_state,
        )


def discover_research_universe(
    transport: IGDemoRESTTransport, *, request_budget: int = 30
) -> tuple[dict[str, object], ...]:
    """Discover all 26 symbols without invented EPICs and without quota overrun."""

    if request_budget < len(INITIAL_INSTRUMENT_REGISTRY):
        raise DemoOperatorError("discovery request budget cannot cover the research universe")
    remaining = request_budget
    results: list[dict[str, object]] = []
    for assignment in research_assignments():
        instrument = INITIAL_INSTRUMENT_REGISTRY[assignment.symbol]
        candidates = transport.search_markets(assignment.symbol)
        remaining -= 1
        classification = classify_discovery(instrument, candidates)
        row: dict[str, object] = {
            "symbol": assignment.symbol,
            "display_name": assignment.display_name,
            "classification": classification.value,
            "epic": None,
            "market": None,
            "metadata": None,
        }
        if classification is InstrumentDiscoveryStatus.VERIFIED and remaining > 0:
            epic = str(candidates[0]["epic"])
            metadata = transport.get_market(epic)
            remaining -= 1
            row.update(
                {
                    "epic": metadata.epic,
                    "market": metadata.market_status,
                    "metadata": {
                        "asset_class": metadata.asset_class,
                        "expiry": metadata.expiry,
                        "currency": metadata.currency,
                        "minimum_deal_size": _text_decimal(metadata.minimum_deal_size),
                        "minimum_stop_distance": _text_decimal(metadata.minimum_stop_distance),
                        "decimal_places": metadata.decimal_places,
                        "pip_or_tick_size": _text_decimal(metadata.pip_or_tick_size),
                        "streaming_available": metadata.streaming_available,
                    },
                }
            )
        elif classification is InstrumentDiscoveryStatus.VERIFIED:
            row["classification"] = InstrumentDiscoveryStatus.METADATA_INCOMPLETE.value
        results.append(row)
    return tuple(results)


def _launch_worker(store_path: Path) -> int:
    command = [
        sys.executable,
        "-m",
        "src.ig_trader.demo_operator",
        "worker",
        "--store",
        str(store_path),
    ]
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(command, **kwargs)
    return process.pid


def _position_document(position: object, record: object | None = None) -> dict[str, object]:
    values = {
        "instrument": getattr(position, "instrument_name", None),
        "epic": getattr(position, "epic", None),
        "direction": getattr(getattr(position, "direction", None), "value", None),
        "size": _text_decimal(getattr(position, "size", None)),
        "entry": _text_decimal(getattr(position, "entry_level", None)),
        "stop": _text_decimal(getattr(position, "stop_level", None)),
        "target": _text_decimal(getattr(position, "limit_level", None)),
        "bid": _text_decimal(getattr(position, "bid", None)),
        "offer": _text_decimal(getattr(position, "offer", None)),
        "currency": getattr(position, "currency", None),
        "deal_id": _abbreviated(getattr(position, "deal_id", None)),
        "ownership": "RECONCILED" if record is not None else "UNKNOWN",
    }
    if record is not None:
        request = getattr(record, "request", None)
        if request is not None:
            values["strategy_id"] = getattr(request, "configuration_identity", None)
            created_at = getattr(request, "created_at", None)
            if isinstance(created_at, datetime):
                values["entry_timestamp"] = _timestamp(created_at)
    return {key: value for key, value in values.items() if value is not None}


def _safe_account_label(account_id: str | None) -> str | None:
    if not account_id:
        return None
    return f"Demo account ••••{account_id[-4:]}"


def _abbreviated(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if len(value) <= 8 else f"{value[:4]}…{value[-4:]}"


def _text_decimal(value: object) -> str | None:
    return str(value) if value is not None else None


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _run_command(arguments: argparse.Namespace) -> int:
    controller = DemoRobotController.from_environment(store_path=arguments.store)
    if arguments.command == "preflight":
        print(json.dumps(asdict(controller.preflight()), sort_keys=True))
    elif arguments.command == "start":
        print(json.dumps(asdict(controller.start()), default=str, sort_keys=True))
    elif arguments.command == "stop":
        print(json.dumps(asdict(controller.stop()), default=str, sort_keys=True))
    elif arguments.command == "pause":
        print(json.dumps(asdict(controller.pause()), default=str, sort_keys=True))
    elif arguments.command == "resume":
        print(json.dumps(asdict(controller.resume()), default=str, sort_keys=True))
    elif arguments.command == "kill":
        print(json.dumps(asdict(controller.emergency_kill()), default=str, sort_keys=True))
    elif arguments.command == "release-kill":
        controller.registry.release_kill_for_local_recovery()
        print(json.dumps(asdict(controller.registry.current()), default=str, sort_keys=True))
    elif arguments.command == "flatten":
        print(
            json.dumps({"closed_positions": controller.close_all_demo_positions()}, sort_keys=True)
        )
    elif arguments.command == "discover":
        controller._validate_start_configuration()
        transport = controller.transport_factory()
        controller._verify_account(transport.get_account())
        print(json.dumps(discover_research_universe(transport), sort_keys=True))
    elif arguments.command == "worker":
        controller.worker_loop()
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Local-only IG Demo Operator controller")
    subcommands = result.add_subparsers(dest="command", required=True)
    for command in (
        "preflight",
        "start",
        "pause",
        "resume",
        "stop",
        "kill",
        "release-kill",
        "flatten",
        "discover",
        "worker",
    ):
        item = subcommands.add_parser(command)
        item.add_argument("--store", type=Path, default=DEFAULT_STORE_PATH)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        return _run_command(parser().parse_args(argv))
    except DemoOperatorError as error:
        print(json.dumps({"classification": "FAIL_CLOSED", "reason": str(error)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
