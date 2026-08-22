from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from src.ig_trader.db_bootstrap import apply_required_migrations, load_migration_sources
from src.ig_trader.execution_lease import ExecutionLeaseCoordinator, PostgresExecutionLeaseStore
from src.ig_trader.models import Signal, SignalDirection
from src.ig_trader.shadow_execution import (
    InMemoryShadowStore,
    ShadowExecutionError,
    ShadowLifecycle,
)
from src.ig_trader.shadow_postgres_store import PostgresShadowStore
from src.ig_trader.shadow_runtime import (
    FinalizedMinuteCandle,
    InMemoryShadowEvidenceStore,
    ShadowAccountState,
    ShadowMarketQuote,
    ShadowRuntime,
    ShadowRuntimeError,
)

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
EPIC = "CS.D.EURGBP.MINI.IP"
ROOT = Path(__file__).resolve().parents[1]


class Lease:
    authorized = True
    fencing_token = 7


def quote(
    *, timestamp: datetime = NOW, bid: float = 0.8500, offer: float = 0.8501
) -> ShadowMarketQuote:
    return ShadowMarketQuote(EPIC, bid, offer, timestamp, 1.0, 0.01, 1.0)


def account(*, timestamp: datetime = NOW) -> ShadowAccountState:
    return ShadowAccountState("shadow", "EUR", 10_000.0, 10_000.0, timestamp)


def candle(index: int) -> FinalizedMinuteCandle:
    timestamp = NOW - timedelta(minutes=61 - index)
    return FinalizedMinuteCandle(EPIC, timestamp, 0.8500, 0.8505, 0.8495, 0.8500, 1.0)


def runtime(store: object | None = None) -> ShadowRuntime:
    return ShadowRuntime(
        lease=Lease(),
        store=store or InMemoryShadowStore(7),  # type: ignore[arg-type]
        evidence=InMemoryShadowEvidenceStore(),
    )


def force_s0_signal(monkeypatch: pytest.MonkeyPatch, value: ShadowRuntime) -> None:
    def generate(epic: str, frame: object) -> Signal:
        timestamp = frame.index[-1]  # type: ignore[union-attr]
        return Signal(
            epic,
            SignalDirection.BUY,
            timestamp,
            0.8500,
            "Scalper",
            0.9,
            {"atr": 0.0004},
        )

    monkeypatch.setattr(value.strategy, "generate_signal", generate)


def warm(runtime_value: ShadowRuntime, monkeypatch: pytest.MonkeyPatch, cycle_id: str):
    force_s0_signal(monkeypatch, runtime_value)
    result = None
    for index in range(60):
        result = runtime_value.evaluate_cycle(
            cycle_id=cycle_id if index == 59 else f"warmup-{cycle_id}-{index}",
            quote=quote(),
            candle=candle(index),
            account=account(),
            now=NOW,
        )
    assert result is not None
    return result


def test_runtime_is_permanently_broker_unauthorized() -> None:
    value = runtime()
    source = (Path(__file__).resolve().parents[1] / "src/ig_trader/shadow_runtime.py").read_text(
        encoding="utf-8"
    )

    assert value.authorized is False
    assert value.order_authority is False
    assert value.broker_order_call_count == 0
    assert "ExecutionEngine" not in source
    assert "working_order" not in source


def test_quote_validation_rejects_stale_future_and_crossed_inputs() -> None:
    value = runtime()
    for invalid in (
        quote(timestamp=NOW - timedelta(seconds=11)),
        quote(timestamp=NOW + timedelta(seconds=1)),
        quote(bid=0.8501, offer=0.8500),
    ):
        with pytest.raises(ShadowRuntimeError):
            value.evaluate_cycle(
                cycle_id=str(uuid4()),
                quote=invalid,
                candle=candle(0),
                account=account(),
                now=NOW,
            )


def test_frozen_policy_derives_levels_and_restart_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryShadowStore(7)
    created = warm(runtime(store), monkeypatch, "global-cycle")
    assert created.intent is not None
    assert created.intent.lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED
    assert created.intent.stop_price == pytest.approx(0.8493)
    assert created.intent.target_price == pytest.approx(0.8513)

    restarted = warm(runtime(store), monkeypatch, "global-cycle")
    assert restarted.decision_code == "DUPLICATE_CYCLE"
    assert restarted.intent == created.intent
    assert store.active_position_count() == 1


def test_conflicting_global_cycle_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryShadowStore(7)
    created = warm(runtime(store), monkeypatch, "global-cycle")
    assert created.intent is not None
    value = runtime(store)
    force_s0_signal(monkeypatch, value)

    for index in range(59):
        value.evaluate_cycle(
            cycle_id=f"other-{index}",
            quote=quote(),
            candle=candle(index),
            account=account(),
            now=NOW,
        )
    with pytest.raises(ShadowExecutionError, match="duplicate"):
        value.evaluate_cycle(
            cycle_id="global-cycle",
            quote=quote(offer=0.85011),
            candle=candle(59),
            account=account(),
            now=NOW,
        )
    assert store.get(created.intent.intent_id) == created.intent


def test_runtime_advances_created_open_closed_and_reconciled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryShadowStore(7)
    created = warm(runtime(store), monkeypatch, "restart-cycle").intent
    assert created is not None

    after_created = runtime(store).advance(created.intent_id, quote(), now=NOW)
    assert after_created.lifecycle is ShadowLifecycle.OPEN
    after_open = runtime(store).advance(
        created.intent_id,
        quote(bid=0.8514, offer=0.8515),
        now=NOW,
    )
    assert after_open.lifecycle is ShadowLifecycle.CLOSED
    after_closed = runtime(store).advance(created.intent_id, quote(), now=NOW)
    assert after_closed.lifecycle is ShadowLifecycle.RECONCILED
    assert runtime(store).advance(created.intent_id, quote(), now=NOW) == after_closed


def test_postgres_shadow_runtime_lifecycle_and_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("disposable PostgreSQL integration gate is disabled")
    admin_dsn = os.environ.get("TEST_POSTGRES_DSN", "").strip()
    if not admin_dsn:
        pytest.skip("disposable PostgreSQL DSN is unavailable")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    values = conninfo_to_dict(admin_dsn)
    if values.get("host") not in {"127.0.0.1", "localhost"}:
        pytest.fail("shadow runtime test refuses non-loopback PostgreSQL")
    if values.get("dbname") != "postgres" or values.get("user") != "postgres":
        pytest.fail("shadow runtime test requires the disposable CI database")
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS ig_trader")
        admin.execute("CREATE DATABASE ig_trader")
    app_dsn = make_conninfo(admin_dsn, dbname="ig_trader")

    def connection_factory():
        return psycopg.connect(app_dsn)

    with connection_factory() as connection:
        apply_required_migrations(
            connection,
            load_migration_sources(ROOT / "migrations" / "postgresql"),
            "ephemeral-shadow-runtime-test",
        )
    lease_store = PostgresExecutionLeaseStore(connection_factory)
    leader = ExecutionLeaseCoordinator(
        store=lease_store,
        replica_instance_id="shadow-runtime-leader",
        execution_enabled=True,
        ttl_seconds=30,
    )
    assert leader.try_acquire()
    store = PostgresShadowStore(leader, connection_factory)
    created_runtime = ShadowRuntime(
        lease=leader,
        store=store,
        evidence=InMemoryShadowEvidenceStore(),
    )
    created = warm(created_runtime, monkeypatch, "runtime-cycle").intent
    assert created is not None
    assert created_runtime.authorized is False
    assert created_runtime.order_authority is False
    assert created_runtime.broker_order_call_count == 0
    assert (
        warm(
            ShadowRuntime(lease=leader, store=store, evidence=InMemoryShadowEvidenceStore()),
            monkeypatch,
            "runtime-cycle",
        ).intent
        == created
    )

    stale_lease = leader.lease
    assert stale_lease is not None and lease_store.release(stale_lease)
    successor = ExecutionLeaseCoordinator(
        store=lease_store,
        replica_instance_id="shadow-runtime-successor",
        execution_enabled=True,
        ttl_seconds=30,
    )
    assert successor.try_acquire()
    with pytest.raises(ShadowExecutionError):
        created_runtime.advance(created.intent_id, quote(), now=NOW)
    assert leader.authorized is False

    successor_store = PostgresShadowStore(successor, connection_factory)
    restarted = ShadowRuntime(
        lease=successor,
        store=successor_store,
        evidence=InMemoryShadowEvidenceStore(),
    )
    opened = restarted.advance(created.intent_id, quote(), now=NOW)
    assert opened.lifecycle is ShadowLifecycle.OPEN
    limited = warm(
        ShadowRuntime(
            lease=successor, store=successor_store, evidence=InMemoryShadowEvidenceStore()
        ),
        monkeypatch,
        "one-position-limit",
    )
    assert limited.intent is None and limited.decision_code == "TOTAL_POSITION_LIMIT"
    closed = restarted.advance(
        created.intent_id,
        quote(bid=0.8514, offer=0.8515),
        now=NOW,
    )
    assert closed.lifecycle is ShadowLifecycle.CLOSED
    assert (
        restarted.advance(created.intent_id, quote(), now=NOW).lifecycle
        is ShadowLifecycle.RECONCILED
    )
