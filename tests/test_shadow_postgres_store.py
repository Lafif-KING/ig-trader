import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from src.ig_trader.db_bootstrap import apply_required_migrations, load_migration_sources
from src.ig_trader.execution_lease import (
    ExecutionLeaseCoordinator,
    FencedOperation,
    PostgresExecutionLeaseStore,
)
from src.ig_trader.shadow_execution import (
    ExecutionMode,
    InstrumentRegistry,
    MarketQuote,
    ShadowExecutionCore,
    ShadowExecutionError,
    ShadowIntentRecord,
    ShadowLifecycle,
)
from src.ig_trader.shadow_postgres_store import PostgresShadowStore

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations" / "postgresql"
NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
EPIC = "CS.D.EURGBP.MINI.IP"


def _local_dsn() -> str:
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("disposable PostgreSQL integration gate is disabled")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("disposable PostgreSQL DSN is unavailable")
    from psycopg.conninfo import conninfo_to_dict

    values = conninfo_to_dict(dsn)
    if values.get("host") not in {"127.0.0.1", "localhost"}:
        pytest.fail("shadow-store test refuses non-loopback PostgreSQL")
    if values.get("dbname") != "postgres" or values.get("user") != "postgres":
        pytest.fail("shadow-store test requires the disposable CI database")
    return dsn


def _signal() -> SimpleNamespace:
    return SimpleNamespace(direction=SimpleNamespace(value="BUY"), strategy_name="S0", epic=EPIC)


def _record(intent_id: UUID, fencing_token: int) -> ShadowIntentRecord:
    return ShadowIntentRecord(
        shadow_position_id=uuid5(NAMESPACE_URL, f"ig-trader-shadow:{intent_id}"),
        intent_id=intent_id,
        strategy_id="S0",
        instrument=EPIC,
        direction="BUY",
        entry_price=0.8500,
        stop_price=0.8490,
        target_price=0.8510,
        fencing_token=fencing_token,
        created_at=NOW,
        updated_at=NOW,
    )


def test_postgres_shadow_store_proof_is_wired_into_ci() -> None:
    workflow = (ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8")

    assert "tests-g4c-shadow-postgres-store.xml" in workflow
    assert "test_postgres_shadow_store_lifecycle_restart_and_fencing" in workflow
    assert 'RUN_POSTGRES_INTEGRATION: "1"' in workflow


def test_postgres_shadow_store_lifecycle_restart_and_fencing() -> None:
    admin_dsn = _local_dsn()
    import psycopg
    from psycopg.conninfo import make_conninfo

    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS ig_trader")
        admin.execute("CREATE DATABASE ig_trader")
    app_dsn = make_conninfo(admin_dsn, dbname="ig_trader")

    def connection_factory():
        return psycopg.connect(app_dsn)

    with connection_factory() as connection:
        apply_required_migrations(
            connection,
            load_migration_sources(MIGRATIONS),
            "ephemeral-shadow-store-test",
        )

    lease_store = PostgresExecutionLeaseStore(connection_factory)
    coordinator = ExecutionLeaseCoordinator(
        store=lease_store,
        replica_instance_id="shadow-store-leader",
        execution_enabled=True,
        ttl_seconds=30,
    )
    assert coordinator.try_acquire()
    store = PostgresShadowStore(coordinator, connection_factory)
    core = ShadowExecutionCore(
        mode=ExecutionMode.SHADOW_DEMO,
        lease=coordinator,
        store=store,
        risk_gate=lambda *_a, **_k: True,
        instruments=InstrumentRegistry.frozen_v1(),
    )
    intent = core.create_intent(
        _signal(),
        MarketQuote(0.8498, 0.8500, NOW),
        stop_price=0.8490,
        target_price=0.8510,
        open_positions_for_strategy=0,
        daily_loss_pct=0,
        now=NOW,
    )
    assert store.active_position_count() == 1
    assert store.get(intent.intent_id) == intent
    assert (
        core.create_intent(
            _signal(),
            MarketQuote(0.8498, 0.8500, NOW),
            intent_id=intent.intent_id,
            stop_price=0.8490,
            target_price=0.8510,
            open_positions_for_strategy=0,
            daily_loss_pct=0,
            now=NOW,
        )
        == intent
    )
    with pytest.raises(ShadowExecutionError, match="duplicate"):
        core.create_intent(
            _signal(),
            MarketQuote(0.8498, 0.8500, NOW),
            intent_id=intent.intent_id,
            stop_price=0.8490,
            target_price=0.8520,
            open_positions_for_strategy=0,
            daily_loss_pct=0,
            now=NOW,
        )

    restarted_store = PostgresShadowStore(coordinator, connection_factory)
    created = restarted_store.get(intent.intent_id)
    assert created is not None and created.lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED
    opened = core.open_intent(created, now=NOW)
    assert restarted_store.active_position_count() == 1
    assert restarted_store.get(intent.intent_id) == opened
    closed = core.close_on_quote(
        opened,
        MarketQuote(0.8510, 0.8512, NOW),
        now=NOW,
    )
    assert closed.lifecycle is ShadowLifecycle.CLOSED
    assert restarted_store.active_position_count() == 0
    closed_after_restart = PostgresShadowStore(coordinator, connection_factory).get(
        intent.intent_id
    )
    assert closed_after_restart == closed
    reconciled = core.reconcile(closed_after_restart, now=NOW)
    assert reconciled.lifecycle is ShadowLifecycle.RECONCILED

    stale_lease = coordinator.lease
    assert stale_lease is not None
    handoff_retry = store.put(_record(uuid4(), stale_lease.fencing_token))
    assert coordinator.release()
    successor = ExecutionLeaseCoordinator(
        store=lease_store,
        replica_instance_id="shadow-store-successor",
        execution_enabled=True,
        ttl_seconds=30,
    )
    assert successor.try_acquire()
    successor_store = PostgresShadowStore(successor, connection_factory)
    stale_intent_id = uuid4()
    stale_record = replace(
        intent,
        shadow_position_id=uuid4(),
        intent_id=stale_intent_id,
        fencing_token=stale_lease.fencing_token,
    )
    with pytest.raises(ShadowExecutionError, match="stale"):
        successor_store.put(stale_record)
    with connection_factory() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM trading.trade_intents WHERE intent_id = %s",
                (stale_intent_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM trading.shadow_position_state WHERE intent_id = %s",
                (stale_intent_id,),
            ).fetchone()[0]
            == 0
        )

    stale_coordinator = SimpleNamespace(
        authorized=True,
        lease=stale_lease,
        run_state_change=lambda operation, callback: lease_store.run_fenced(
            stale_lease, operation, callback
        ),
    )
    stale_store = PostgresShadowStore(stale_coordinator, connection_factory)
    with pytest.raises(ShadowExecutionError):
        stale_store.transition(
            intent.intent_id,
            ShadowLifecycle.RECONCILED,
            ShadowLifecycle.CLOSED,
            stale_lease.fencing_token,
            updated_at=NOW,
        )

    with connection_factory() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM trading.shadow_position_state WHERE intent_id = %s",
                (intent.intent_id,),
            ).fetchone()[0]
            == 1
        )

    successor_lease = successor.lease
    assert successor_lease is not None
    successor_core = ShadowExecutionCore(
        mode=ExecutionMode.SHADOW_DEMO,
        lease=successor,
        store=successor_store,
        risk_gate=lambda *_a, **_k: True,
        instruments=InstrumentRegistry.frozen_v1(),
    )
    retried_after_handoff = successor_store.put(
        replace(handoff_retry, fencing_token=successor_lease.fencing_token)
    )
    assert retried_after_handoff == handoff_retry
    with pytest.raises(ShadowExecutionError, match="duplicate"):
        successor_store.put(
            replace(
                handoff_retry,
                fencing_token=successor_lease.fencing_token,
                target_price=0.8520,
            )
        )
    opened_handoff = successor_core.open_intent(retried_after_handoff, now=NOW)
    closed_handoff = successor_core.close_on_quote(
        opened_handoff,
        MarketQuote(0.8510, 0.8512, NOW),
        now=NOW,
    )
    successor_core.reconcile(closed_handoff, now=NOW)

    concurrent_records = (
        _record(uuid4(), successor_lease.fencing_token),
        _record(uuid4(), successor_lease.fencing_token),
    )

    def attempt(record: ShadowIntentRecord) -> tuple[bool, ShadowIntentRecord | None]:
        try:
            return True, successor_store.put(record)
        except ShadowExecutionError:
            return False, None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, concurrent_records))
    assert sum(saved for saved, _record_value in outcomes) == 1
    winner = next(record for saved, record in outcomes if saved)
    assert winner is not None
    with connection_factory() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM trading.trade_intents WHERE intent_id IN (%s, %s)",
                tuple(record.intent_id for record in concurrent_records),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM trading.shadow_position_state WHERE intent_id IN (%s, %s)",
                tuple(record.intent_id for record in concurrent_records),
            ).fetchone()[0]
            == 0
        )
    opened_winner = successor_core.open_intent(winner, now=NOW)
    closed_winner = successor_core.close_on_quote(
        opened_winner,
        MarketQuote(0.8510, 0.8512, NOW),
        now=NOW,
    )
    successor_core.reconcile(closed_winner, now=NOW)

    retry_id = uuid4()
    retry = _record(retry_id, successor_lease.fencing_token)
    assert successor_store.put(retry) == retry

    opened_retry = successor_core.open_intent(retry, now=NOW)

    def mutate(statement: str, parameters: tuple[object, ...]) -> None:
        successor.run_state_change(
            FencedOperation.RECONCILIATION,
            lambda cursor: cursor.execute(statement, parameters),
        )

    mutate(
        "UPDATE trading.trade_intents SET lifecycle_state = 'CLOSED' WHERE intent_id = %s",
        (retry_id,),
    )
    with pytest.raises(ShadowExecutionError, match="lifecycle"):
        successor_store.get(retry_id)
    mutate(
        "UPDATE trading.trade_intents SET lifecycle_state = 'OPEN' WHERE intent_id = %s",
        (retry_id,),
    )
    mutate(
        """
        UPDATE trading.trade_intents
        SET intent_payload = jsonb_set(intent_payload, '{entry_price}', '0.1'::jsonb)
        WHERE intent_id = %s
        """,
        (retry_id,),
    )
    with pytest.raises(ShadowExecutionError, match="payload"):
        successor_store.get(retry_id)
    mutate(
        """
        UPDATE trading.trade_intents
        SET intent_payload = jsonb_set(intent_payload, '{entry_price}', '0.85'::jsonb)
        WHERE intent_id = %s
        """,
        (retry_id,),
    )
    closed_retry = successor_core.close_on_quote(
        opened_retry,
        MarketQuote(0.8510, 0.8512, NOW),
        now=NOW,
    )
    successor_core.reconcile(closed_retry, now=NOW)

    failed = successor_store.put(_record(uuid4(), successor_lease.fencing_token))
    opened_failed = successor_core.open_intent(failed, now=NOW)
    failed = successor_store.transition(
        opened_failed.intent_id,
        ShadowLifecycle.OPEN,
        ShadowLifecycle.FAILED_SAFE,
        successor_lease.fencing_token,
        updated_at=NOW,
    )
    assert failed.lifecycle is ShadowLifecycle.FAILED_SAFE
    with pytest.raises(ShadowExecutionError, match="unresolved"):
        successor_store.put(_record(uuid4(), successor_lease.fencing_token))
    assert successor_store.active_position_count() == 1

    with connection_factory() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'trading' AND table_name = 'shadow_position_state'"
            ).fetchall()
        }
        assert {"deal_id", "order_id", "working_order_id"}.isdisjoint(columns)
        assert (
            connection.execute(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgname = 'shadow_position_state_require_fence'"
            ).fetchone()[0]
            == 1
        )
