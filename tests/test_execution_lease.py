"""G4B-02B1 execution lease, fencing, identity, and PostgreSQL proofs."""

from __future__ import annotations

import multiprocessing
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.ig_trader.cloud_config import CloudConfig
from src.ig_trader.execution_lease import (
    EXECUTION_LEASE_NAME,
    POSTGRES_ENTRA_SCOPE,
    ExecutionLeaseCoordinator,
    FencedOperation,
    FencingRejected,
    LeaseDatabaseError,
    LeaseRecord,
    LeaseState,
    ManagedIdentityPostgresConfig,
    ManagedIdentityPostgresConnectionFactory,
    PostgresExecutionLeaseStore,
    RuntimeRole,
    StatefulWorkProhibited,
    UnsafePostgresConfiguration,
    no_execution_lease_status,
)

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_DSN_ENV = "TEST_POSTGRES_DSN"
POSTGRES_INTEGRATION_ENV = "RUN_POSTGRES_INTEGRATION"
POSTGRES_TEST_ROLE = "g4b02b1_runtime_test"


class MemoryLeaseStore:
    """Deterministic contract double; not a PostgreSQL concurrency substitute."""

    def __init__(self) -> None:
        self.current: LeaseRecord | None = None
        self.next_token = 1
        self.acquire_failure = False
        self.renew_failure = False
        self.ambiguous_fence = False

    def acquire(
        self,
        lease_name: str,
        holder_instance_id: str,
        ttl_seconds: float,
    ) -> LeaseRecord | None:
        if self.acquire_failure:
            raise LeaseDatabaseError("unavailable")
        if self.current is not None:
            return None
        now = datetime.now(UTC)
        self.current = LeaseRecord(
            lease_name=lease_name,
            holder_instance_id=holder_instance_id,
            fencing_token=self.next_token,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        self.next_token += 1
        return self.current

    def renew(self, lease: LeaseRecord, ttl_seconds: float) -> LeaseRecord | None:
        if self.renew_failure:
            raise LeaseDatabaseError("unavailable")
        if self.current != lease:
            return None
        now = datetime.now(UTC)
        self.current = LeaseRecord(
            lease_name=lease.lease_name,
            holder_instance_id=lease.holder_instance_id,
            fencing_token=lease.fencing_token,
            acquired_at=lease.acquired_at,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        return self.current

    def release(self, lease: LeaseRecord) -> bool:
        if self.current != lease:
            return False
        self.current = None
        return True

    def run_fenced(
        self,
        lease: LeaseRecord,
        operation: FencedOperation,
        callback: Callable[[Any], Any],
    ) -> Any:
        del operation
        if self.ambiguous_fence or self.current != lease:
            raise FencingRejected("stale")
        return callback(None)

    def expire(self) -> None:
        self.current = None


def _coordinator(
    store: MemoryLeaseStore,
    instance: str,
    *,
    enabled: bool = True,
) -> ExecutionLeaseCoordinator:
    return ExecutionLeaseCoordinator(
        store=store,
        replica_instance_id=instance,
        execution_enabled=enabled,
        ttl_seconds=2,
    )


def test_no_execution_is_explicitly_unauthorized_without_a_lease() -> None:
    status = no_execution_lease_status("replica-a")

    assert status.runtime_role is RuntimeRole.NO_EXECUTION
    assert status.authorized is False
    assert status.lease_holder is False
    assert status.fencing_token is None
    assert status.document() == {
        "authorized": False,
        "fencing_token": None,
        "lease_heartbeat_state": "DISABLED",
        "lease_holder": False,
        "lease_name": "execution-worker",
        "lease_state": "DISABLED",
        "replica_instance_id": "replica-a",
        "runtime_role": "NO_EXECUTION",
    }


def test_replica_identity_is_stable_and_distinguishes_replicas() -> None:
    first = CloudConfig.from_environment({"HOSTNAME": "replica-a"})
    repeated = CloudConfig.from_environment({"HOSTNAME": "replica-a"})
    second = CloudConfig.from_environment({"HOSTNAME": "replica-b"})

    assert first.replica_instance_id == repeated.replica_instance_id
    assert first.replica_instance_id != second.replica_instance_id


def test_one_replica_acquires_and_second_replica_is_standby() -> None:
    store = MemoryLeaseStore()
    first = _coordinator(store, "replica-a")
    second = _coordinator(store, "replica-b")

    assert first.try_acquire() is True
    assert second.try_acquire() is False
    assert first.role is RuntimeRole.LEADER
    assert first.authorized is True
    assert second.role is RuntimeRole.STANDBY
    assert second.authorized is False


def test_two_replicas_cannot_both_become_leader() -> None:
    store = MemoryLeaseStore()
    replicas = [_coordinator(store, "replica-a"), _coordinator(store, "replica-b")]

    outcomes = [replica.try_acquire() for replica in replicas]

    assert outcomes.count(True) == 1
    assert sum(replica.role is RuntimeRole.LEADER for replica in replicas) == 1


def test_lease_renewal_keeps_the_same_fencing_token() -> None:
    store = MemoryLeaseStore()
    leader = _coordinator(store, "replica-a")
    assert leader.try_acquire()
    original = leader.lease

    assert leader.renew() is True
    assert leader.lease is not None
    assert original is not None
    assert leader.lease.fencing_token == original.fencing_token
    assert leader.lease.heartbeat_at >= original.heartbeat_at


def test_lease_expiration_allows_clean_handoff_with_newer_fence() -> None:
    store = MemoryLeaseStore()
    former = _coordinator(store, "replica-a")
    successor = _coordinator(store, "replica-b")
    assert former.try_acquire()
    old_token = former.lease.fencing_token if former.lease else 0

    store.expire()
    assert successor.try_acquire()

    assert successor.lease is not None
    assert successor.lease.fencing_token > old_token
    assert successor.role is RuntimeRole.LEADER


def test_clean_release_allows_immediate_handoff() -> None:
    store = MemoryLeaseStore()
    leader = _coordinator(store, "replica-a")
    follower = _coordinator(store, "replica-b")
    assert leader.try_acquire()
    old_token = leader.lease.fencing_token if leader.lease else 0

    assert leader.release() is True
    assert leader.role is RuntimeRole.STANDBY
    assert follower.try_acquire() is True
    assert follower.lease is not None
    assert follower.lease.fencing_token > old_token


def test_database_disconnect_during_acquire_is_fail_closed() -> None:
    store = MemoryLeaseStore()
    store.acquire_failure = True
    replica = _coordinator(store, "replica-a")

    assert replica.try_acquire() is False
    assert replica.role is RuntimeRole.STANDBY
    assert replica.authorized is False
    assert replica.lease_state is LeaseState.DATABASE_UNAVAILABLE


def test_database_disconnect_during_renewal_immediately_demotes_leader() -> None:
    store = MemoryLeaseStore()
    replica = _coordinator(store, "replica-a")
    assert replica.try_acquire()
    store.renew_failure = True

    assert replica.renew() is False
    assert replica.role is RuntimeRole.STANDBY
    assert replica.authorized is False
    assert replica.lease is None


def test_ambiguous_fence_state_stops_stateful_work_and_demotes() -> None:
    store = MemoryLeaseStore()
    replica = _coordinator(store, "replica-a")
    assert replica.try_acquire()
    store.ambiguous_fence = True

    with pytest.raises(StatefulWorkProhibited):
        replica.run_state_change(FencedOperation.TRADE_INTENT, lambda _cursor: True)

    assert replica.role is RuntimeRole.STANDBY
    assert replica.authorized is False


@pytest.mark.parametrize("operation", list(FencedOperation))
def test_stale_fencing_token_rejects_every_protected_operation(
    operation: FencedOperation,
) -> None:
    store = MemoryLeaseStore()
    former = _coordinator(store, "replica-a")
    successor = _coordinator(store, "replica-b")
    assert former.try_acquire()
    store.expire()
    assert successor.try_acquire()

    with pytest.raises(StatefulWorkProhibited):
        former.run_state_change(operation, lambda _cursor: True)

    assert former.role is RuntimeRole.STANDBY


def test_no_execution_coordinator_never_contacts_postgresql() -> None:
    store = MemoryLeaseStore()
    store.acquire_failure = True
    runtime = _coordinator(store, "replica-a", enabled=False)

    assert runtime.try_acquire() is False
    assert runtime.role is RuntimeRole.NO_EXECUTION
    assert runtime.status().authorized is False
    assert runtime.status().lease_state is LeaseState.DISABLED


@pytest.mark.parametrize(
    "unsafe",
    [
        {"DATABASE_URL": "sqlite:///local.db"},
        {"POSTGRES_PASSWORD": "not-accepted"},
        {"PGPASSWORD": "not-accepted"},
    ],
)
def test_cloud_postgresql_rejects_passwords_urls_and_sqlite_fallback(
    unsafe: dict[str, str],
) -> None:
    environment = {
        "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000001",
        "POSTGRES_HOST": "example.postgres.database.azure.com",
        **unsafe,
    }

    with pytest.raises(UnsafePostgresConfiguration):
        ManagedIdentityPostgresConfig.from_environment(environment)


def test_managed_identity_postgresql_uses_memory_token_and_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psycopg

    calls: list[dict[str, Any]] = []

    class TokenProvider:
        def get_token(self) -> str:
            return "opaque-value"

    sentinel = object()

    def connect(**kwargs: Any) -> object:
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(psycopg, "connect", connect)
    config = ManagedIdentityPostgresConfig.from_environment(
        {
            "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000001",
            "POSTGRES_HOST": "example.postgres.database.azure.com",
        }
    )
    factory = ManagedIdentityPostgresConnectionFactory(config, TokenProvider())  # type: ignore[arg-type]

    assert factory() is sentinel
    assert calls[0]["password"] == "opaque-value"
    assert calls[0]["sslmode"] == "require"
    assert calls[0]["user"] == "igtrdevfrc-execution-identity"
    assert POSTGRES_ENTRA_SCOPE.endswith("/.default")
    assert "opaque-value" not in repr(factory)


def test_cloud_lease_module_contains_no_sqlite_fallback() -> None:
    source = (ROOT / "src/ig_trader/execution_lease.py").read_text(encoding="utf-8")

    assert "sqlite3" not in source
    assert "sqlite:///" not in source
    assert "POSTGRES_PASSWORD" in source


def test_migration_extends_the_existing_worker_lease_and_fences_state_tables() -> None:
    initial = (ROOT / "migrations/postgresql/001_execution_state.sql").read_text(encoding="utf-8")
    migration = (ROOT / "migrations/postgresql/002_execution_lease_fencing.sql").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS trading.worker_leases" in initial
    assert "ALTER TABLE trading.worker_leases" in migration
    assert "worker_lease_fencing_token_seq" in migration
    assert "acquire_execution_lease" in migration
    assert "renew_execution_lease" in migration
    assert "release_execution_lease" in migration
    assert "assert_execution_fence" in migration
    assert "require_current_execution_fence" in migration
    for table in (
        "execution_cycle_claims",
        "trade_intents",
        "lifecycle_events",
        "broker_references",
        "position_state",
        "reconciliation_state",
        "evidence_metadata",
    ):
        assert f"ON trading.{table}" in migration


def _postgres_connection(dsn: str) -> Any:
    import psycopg

    return psycopg.connect(
        psycopg.conninfo.make_conninfo(
            dsn,
            user=POSTGRES_TEST_ROLE,
            connect_timeout=5,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        )
    )


def _prepare_postgres(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            (ROOT / "migrations/postgresql/001_execution_state.sql").read_text(encoding="utf-8")
        )
        connection.execute(
            (ROOT / "migrations/postgresql/002_execution_lease_fencing.sql").read_text(
                encoding="utf-8"
            )
        )
        connection.execute("TRUNCATE trading.execution_cycle_claims")
        connection.execute("TRUNCATE trading.worker_leases")
        connection.execute("ALTER SEQUENCE trading.worker_lease_fencing_token_seq RESTART WITH 1")
        connection.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{POSTGRES_TEST_ROLE}') THEN
                    CREATE ROLE {POSTGRES_TEST_ROLE} LOGIN;
                END IF;
            END;
            $$;
            REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA trading
            FROM {POSTGRES_TEST_ROLE};
            REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA trading
            FROM {POSTGRES_TEST_ROLE};
            GRANT USAGE ON SCHEMA trading TO {POSTGRES_TEST_ROLE};
            GRANT SELECT ON trading.worker_leases, trading.execution_cycle_claims
            TO {POSTGRES_TEST_ROLE};
            GRANT INSERT, UPDATE ON trading.execution_cycle_claims
            TO {POSTGRES_TEST_ROLE};
            GRANT EXECUTE ON FUNCTION
                trading.acquire_execution_lease(text, text, double precision),
                trading.renew_execution_lease(text, text, bigint, double precision),
                trading.release_execution_lease(text, text, bigint),
                trading.assert_execution_fence(text, text, bigint, text),
                trading.require_current_execution_fence()
            TO {POSTGRES_TEST_ROLE};
            """
        )


def _lease_process(
    dsn: str,
    instance_id: str,
    ttl_seconds: float,
    start: Any,
    hold: Any,
    result: Any,
) -> None:
    try:
        store = PostgresExecutionLeaseStore(lambda: _postgres_connection(dsn))
        start.wait(15)
        lease = store.acquire(EXECUTION_LEASE_NAME, instance_id, ttl_seconds)
        result.send(
            {
                "instance": instance_id,
                "role": "LEADER" if lease else "STANDBY",
                "token": lease.fencing_token if lease else None,
            }
        )
        if lease is not None:
            hold.wait(30)
    finally:
        result.close()


def _start_lease_process(
    context: Any,
    *,
    dsn: str,
    instance_id: str,
    ttl_seconds: float,
    start: Any,
    hold: Any,
) -> tuple[Any, Any]:
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_lease_process,
        args=(dsn, instance_id, ttl_seconds, start, hold, sender),
    )
    process.start()
    sender.close()
    return process, receiver


def _stop_process(process: Any) -> None:
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)


def _required_postgres_dsn() -> str:
    if os.environ.get(POSTGRES_INTEGRATION_ENV) != "1":
        pytest.skip("real PostgreSQL runs only in the dedicated bounded CI gate")
    dsn = os.environ.get(POSTGRES_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip("real PostgreSQL is required; remote CI provides PostgreSQL 16")
    return dsn


def test_two_processes_elect_one_leader_then_handoff_after_leader_crash() -> None:
    dsn = _required_postgres_dsn()
    _prepare_postgres(dsn)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    hold = context.Event()
    processes: dict[str, Any] = {}
    receivers: dict[str, Any] = {}
    successor: Any = None
    successor_receiver: Any = None
    successor_hold: Any = None
    try:
        for instance in ("process-a", "process-b"):
            process, receiver = _start_lease_process(
                context,
                dsn=dsn,
                instance_id=instance,
                ttl_seconds=2.0,
                start=start,
                hold=hold,
            )
            processes[instance] = process
            receivers[instance] = receiver

        start.set()
        assert all(receiver.poll(20) for receiver in receivers.values())
        initial = [receiver.recv() for receiver in receivers.values()]

        assert sorted(item["role"] for item in initial) == ["LEADER", "STANDBY"]
        leader_result = next(item for item in initial if item["role"] == "LEADER")
        follower_result = next(item for item in initial if item["role"] == "STANDBY")
        leader_process = processes[leader_result["instance"]]
        leader_process.terminate()
        leader_process.join(timeout=10)
        assert not leader_process.is_alive()
        hold.set()
        follower_process = processes[follower_result["instance"]]
        follower_process.join(timeout=10)
        assert follower_process.exitcode == 0

        time.sleep(2.2)
        successor_start = context.Event()
        successor_hold = context.Event()
        successor, successor_receiver = _start_lease_process(
            context,
            dsn=dsn,
            instance_id=follower_result["instance"],
            ttl_seconds=2.0,
            start=successor_start,
            hold=successor_hold,
        )
        successor_start.set()
        assert successor_receiver.poll(20)
        handoff = successor_receiver.recv()
        successor_hold.set()
        successor.join(timeout=10)

        assert successor.exitcode == 0
        assert handoff["role"] == "LEADER"
        assert handoff["instance"] == follower_result["instance"]
        assert handoff["token"] > leader_result["token"]
    finally:
        hold.set()
        if successor_hold is not None:
            successor_hold.set()
        for process in processes.values():
            _stop_process(process)
        if successor is not None:
            _stop_process(successor)
        for receiver in receivers.values():
            receiver.close()
        if successor_receiver is not None:
            successor_receiver.close()


def test_postgresql_rejects_unfenced_and_stale_state_changes() -> None:
    dsn = _required_postgres_dsn()
    _prepare_postgres(dsn)
    store = PostgresExecutionLeaseStore(lambda: _postgres_connection(dsn))
    leader = store.acquire(EXECUTION_LEASE_NAME, "process-a", 5)
    assert leader is not None

    import psycopg

    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        _postgres_connection(dsn) as connection,
    ):
        connection.execute(
            """
            UPDATE trading.worker_leases
            SET lease_until = clock_timestamp()
            WHERE lease_name = 'execution-worker'
            """
        )

    with pytest.raises(psycopg.Error), _postgres_connection(dsn) as connection:
        connection.execute(
            """
                INSERT INTO trading.execution_cycle_claims (
                    cycle_id, lease_name, holder_instance_id,
                    fencing_token, claimed_at, state
                ) VALUES (
                    '00000000-0000-0000-0000-000000000001',
                    'execution-worker', 'process-a', %s, clock_timestamp(), 'CLAIMED'
                )
                """,
            (leader.fencing_token,),
        )

    def create_claim(cursor: Any) -> None:
        cursor.execute(
            """
            INSERT INTO trading.execution_cycle_claims (
                cycle_id, lease_name, holder_instance_id,
                fencing_token, claimed_at, state
            ) VALUES (
                '00000000-0000-0000-0000-000000000001',
                'execution-worker', 'process-a', %s, clock_timestamp(), 'CLAIMED'
            )
            """,
            (leader.fencing_token,),
        )

    store.run_fenced(leader, FencedOperation.CYCLE_OWNERSHIP, create_claim)
    assert store.release(leader)
    successor = store.acquire(EXECUTION_LEASE_NAME, "process-b", 5)
    assert successor is not None
    assert successor.fencing_token > leader.fencing_token

    with pytest.raises(FencingRejected):
        store.run_fenced(leader, FencedOperation.RECONCILIATION, lambda _cursor: None)
