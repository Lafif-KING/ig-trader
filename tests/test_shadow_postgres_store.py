import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ig_trader.db_bootstrap import apply_required_migrations, load_migration_sources
from src.ig_trader.execution_lease import (
    ExecutionLeaseCoordinator,
    PostgresExecutionLeaseStore,
)
from src.ig_trader.shadow_execution import (
    ExecutionMode,
    InstrumentRegistry,
    MarketQuote,
    ShadowExecutionCore,
    ShadowExecutionError,
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
    assert restarted_store.get(intent.intent_id) == opened
    closed = core.close_on_quote(
        opened,
        MarketQuote(0.8510, 0.8512, NOW),
        now=NOW,
    )
    assert closed.lifecycle is ShadowLifecycle.CLOSED
    closed_after_restart = PostgresShadowStore(coordinator, connection_factory).get(
        intent.intent_id
    )
    assert closed_after_restart == closed
    reconciled = core.reconcile(closed_after_restart, now=NOW)
    assert reconciled.lifecycle is ShadowLifecycle.RECONCILED

    stale_lease = coordinator.lease
    assert stale_lease is not None and coordinator.release()
    successor = ExecutionLeaseCoordinator(
        store=lease_store,
        replica_instance_id="shadow-store-successor",
        execution_enabled=True,
        ttl_seconds=30,
    )
    assert successor.try_acquire()
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
