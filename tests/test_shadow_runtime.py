from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta, timezone
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
    FinalizedCandleBuffer,
    FinalizedMinuteCandle,
    InMemoryShadowEvidenceStore,
    ShadowAccountState,
    ShadowMarketQuote,
    ShadowRuntime,
    ShadowRuntimeError,
)

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
EPIC = "CS.D.EURGBP.MINI.IP"
EURUSD_EPIC = "CS.D.EURUSD.MINI.IP"
GBPUSD_EPIC = "CS.D.GBPUSD.MINI.IP"
ROOT = Path(__file__).resolve().parents[1]


class Lease:
    authorized = True
    fencing_token = 7


def quote(
    *,
    epic: str = EPIC,
    timestamp: datetime = NOW,
    bid: float = 0.8500,
    offer: float = 0.8501,
    pip_value_account_currency: float = 1.0,
    minimum_size: float = 0.01,
    minimum_stop_pips: float = 1.0,
) -> ShadowMarketQuote:
    return ShadowMarketQuote(
        epic,
        bid,
        offer,
        timestamp,
        pip_value_account_currency,
        minimum_size,
        minimum_stop_pips,
    )


def account(
    *,
    timestamp: datetime = NOW,
    balance: float = 10_000.0,
    starting_balance: float = 10_000.0,
    state_known: bool = True,
) -> ShadowAccountState:
    return ShadowAccountState("shadow", "EUR", balance, starting_balance, timestamp, state_known)


def candle(
    index: int,
    *,
    epic: str = EPIC,
    timestamp: datetime | None = None,
) -> FinalizedMinuteCandle:
    resolved_timestamp = timestamp or NOW - timedelta(minutes=61 - index)
    return FinalizedMinuteCandle(epic, resolved_timestamp, 0.8500, 0.8505, 0.8495, 0.8500, 1.0)


def runtime(store: object | None = None) -> ShadowRuntime:
    return ShadowRuntime(
        lease=Lease(),
        store=store or InMemoryShadowStore(7),
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


def warm(
    runtime_value: ShadowRuntime,
    monkeypatch: pytest.MonkeyPatch,
    cycle_id: str,
    *,
    epic: str = EPIC,
    account_value: object | None = None,
):
    force_s0_signal(monkeypatch, runtime_value)
    result = None
    for index in range(60):
        result = runtime_value.evaluate_cycle(
            cycle_id=cycle_id if index == 59 else f"warmup-{cycle_id}-{index}",
            quote=quote(epic=epic),
            candle=candle(index, epic=epic),
            account=account() if account_value is None else account_value,  # type: ignore[arg-type]
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
    assert "PostgresShadowStore" not in source
    assert "ShadowStore" in source


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


def test_finalized_candle_buffer_is_bounded_utc_gap_preserving_and_idempotent() -> None:
    buffer = FinalizedCandleBuffer(2)
    first_timestamp = (NOW - timedelta(minutes=61)).astimezone(timezone(timedelta(hours=2)))
    first = candle(
        0,
        timestamp=first_timestamp,
    )
    gap_after_first = candle(2)

    assert buffer.append(first, now=NOW)[0].timestamp.tzinfo is UTC
    accepted = buffer.append(gap_after_first, now=NOW)
    assert [item.timestamp for item in accepted] == [
        NOW - timedelta(minutes=61),
        NOW - timedelta(minutes=59),
    ]
    assert NOW - timedelta(minutes=60) not in [item.timestamp for item in accepted]

    before_duplicate = buffer.snapshot(EPIC)
    with pytest.raises(ShadowRuntimeError, match="already emitted"):
        buffer.append(candle(2), now=NOW)
    assert buffer.snapshot(EPIC) == before_duplicate

    bounded = buffer.append(candle(3), now=NOW)
    assert len(bounded) == 2
    assert [item.timestamp for item in bounded] == [
        NOW - timedelta(minutes=59),
        NOW - timedelta(minutes=58),
    ]


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
    duplicate = warm(runtime(store), monkeypatch, "global-cycle")
    assert duplicate.decision_code == "DUPLICATE_CYCLE"
    assert duplicate.intent == created.intent

    for conflicting_epic in (EURUSD_EPIC, GBPUSD_EPIC):
        with pytest.raises(ShadowExecutionError, match="duplicate"):
            warm(runtime(store), monkeypatch, "global-cycle", epic=conflicting_epic)
    assert store.get(created.intent.intent_id) == created.intent
    assert len(store.records) == 1


def test_portfolio_risk_daily_loss_veto_cannot_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryShadowStore(7)
    value = runtime(store)
    monkeypatch.setattr(value._execution, "risk_gate", lambda *_args, **_kwargs: True)

    result = warm(
        value,
        monkeypatch,
        "daily-loss-veto",
        account_value=account(balance=9_400.0),
    )

    assert result.decision_code == "DAILY_LOSS_LIMIT"
    assert result.intent is None
    assert store.records == {}


def test_portfolio_risk_position_limit_vetoes_without_caller_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryShadowStore(7)
    active = warm(runtime(store), monkeypatch, "active-position")
    assert active.intent is not None

    result = warm(runtime(store), monkeypatch, "position-limit")

    assert result.decision_code == "TOTAL_POSITION_LIMIT"
    assert result.intent is None
    assert len(store.records) == 1


def test_portfolio_risk_missing_account_state_vetoes(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryShadowStore(7)

    result = warm(runtime(store), monkeypatch, "missing-account", account_value=object())

    assert result.decision_code == "ACCOUNT_STATE_UNKNOWN"
    assert result.intent is None
    assert store.records == {}


def test_missing_instrument_metadata_fails_closed_without_intent() -> None:
    store = InMemoryShadowStore(7)
    value = runtime(store)
    unsupported = "UNSUPPORTED"

    with pytest.raises(ShadowExecutionError, match="instrument"):
        value.evaluate_cycle(
            cycle_id="missing-metadata",
            quote=quote(epic=unsupported),
            candle=candle(0, epic=unsupported),
            account=account(),
            now=NOW,
        )
    assert store.records == {}


def test_missing_quote_metadata_fails_closed_without_intent() -> None:
    store = InMemoryShadowStore(7)
    value = runtime(store)
    missing_size = ShadowMarketQuote(EPIC, 0.8500, 0.8501, NOW, 1.0, None, 1.0)

    with pytest.raises(ShadowRuntimeError, match="market quote"):
        value.evaluate_cycle(
            cycle_id="missing-quote-metadata",
            quote=missing_size,
            candle=candle(0),
            account=account(),
            now=NOW,
        )
    assert store.records == {}


def test_invalid_pip_metadata_fails_closed_without_intent() -> None:
    store = InMemoryShadowStore(7)
    value = runtime(store)

    with pytest.raises(ShadowRuntimeError, match="market quote"):
        value.evaluate_cycle(
            cycle_id="invalid-pip-metadata",
            quote=quote(pip_value_account_currency=float("nan")),
            candle=candle(0),
            account=account(),
            now=NOW,
        )
    assert store.records == {}


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
