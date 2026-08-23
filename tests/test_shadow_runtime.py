from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.ig_trader.db_bootstrap import apply_required_migrations, load_migration_sources
from src.ig_trader.execution_lease import ExecutionLeaseCoordinator, PostgresExecutionLeaseStore
from src.ig_trader.frozen_v1_policy import (
    FROZEN_V1_PRODUCTION_INSTRUMENTS,
    G2_HISTORICAL_FIXTURE_INSTRUMENTS,
    FrozenV1Config,
)
from src.ig_trader.models import Signal, SignalDirection
from src.ig_trader.shadow_execution import (
    ExecutionMode,
    InMemoryShadowStore,
    InstrumentRegistry,
    MarketQuote,
    ShadowExecutionCore,
    ShadowExecutionError,
    ShadowLifecycle,
)
from src.ig_trader.shadow_postgres_store import PostgresShadowStore
from src.ig_trader.shadow_runtime import (
    FinalizedCandleBuffer,
    FinalizedMinuteCandle,
    InMemoryShadowEvidenceStore,
    OneShotRiskPermit,
    ShadowAccountState,
    ShadowMarketQuote,
    ShadowRuntime,
    ShadowRuntimeError,
)

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
EPIC = "CS.D.EURGBP.MINI.IP"
EURUSD_EPIC = "CS.D.EURUSD.CEFM.IP"
GBPUSD_EPIC = "CS.D.GBPUSD.MINI.IP"
ROOT = Path(__file__).resolve().parents[1]
G2_CONFIGURATION_HASH = "f858df69e5dbae0703d944a095ec5899035dcf8ab2f0ac96b3a65dde35d8244f"
SHADOW_CONFIGURATION_HASH = "ceb5781096ba61d5cfbf5acbec4fd37329c4f8813f44182d772c9a79ef40c629"


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


def runtime(
    store: object | None = None,
    evidence: InMemoryShadowEvidenceStore | None = None,
) -> ShadowRuntime:
    return ShadowRuntime(
        lease=Lease(),
        store=store or InMemoryShadowStore(7),
        evidence=evidence or InMemoryShadowEvidenceStore(),
    )


def force_s0_signal(
    monkeypatch: pytest.MonkeyPatch,
    value: ShadowRuntime,
    *,
    atr: float = 0.0004,
    calls: list[None] | None = None,
) -> None:
    def generate(epic: str, frame: object) -> Signal:
        if calls is not None:
            calls.append(None)
        timestamp = frame.index[-1]  # type: ignore[union-attr]
        return Signal(
            epic,
            SignalDirection.BUY,
            timestamp,
            0.8500,
            "Scalper",
            0.9,
            {"atr": atr},
        )

    monkeypatch.setattr(value.strategy, "generate_signal", generate)


def warm(
    runtime_value: ShadowRuntime,
    monkeypatch: pytest.MonkeyPatch,
    cycle_id: str,
    *,
    epic: str = EPIC,
    account_value: object | None = None,
    minimum_stop_pips: float = 1.0,
    atr: float = 0.0004,
    bid: float = 0.8500,
    offer: float = 0.8501,
):
    force_s0_signal(monkeypatch, runtime_value, atr=atr)
    result = None
    for index in range(60):
        result = runtime_value.evaluate_cycle(
            cycle_id=cycle_id if index == 59 else f"warmup-{cycle_id}-{index}",
            quote=(
                quote(
                    epic=epic,
                    bid=bid,
                    offer=offer,
                    minimum_stop_pips=minimum_stop_pips,
                )
            ),
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


def test_production_shadow_and_historical_g2_instruments_are_separate() -> None:
    production_epics = tuple(item[1] for item in FROZEN_V1_PRODUCTION_INSTRUMENTS)
    historical_epics = tuple(item[1] for item in G2_HISTORICAL_FIXTURE_INSTRUMENTS)

    assert production_epics == (EPIC, EURUSD_EPIC, GBPUSD_EPIC)
    assert historical_epics == (EPIC, "CS.D.EURUSD.MINI.IP", GBPUSD_EPIC)
    registry = InstrumentRegistry.frozen_v1()
    for epic in production_epics:
        assert registry.require(epic).epic == epic
    with pytest.raises(ShadowExecutionError, match="outside"):
        registry.require("CS.D.EURUSD.MINI.IP")


def test_mode_specific_configuration_hashes_are_exact_and_distinct() -> None:
    config = FrozenV1Config()

    assert config.configuration_hash == G2_CONFIGURATION_HASH
    assert config.shadow_configuration_hash == SHADOW_CONFIGURATION_HASH
    assert config.configuration_hash != config.shadow_configuration_hash


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


def test_finalized_candle_buffer_resets_on_gap_without_synthetic_candles() -> None:
    buffer = FinalizedCandleBuffer(2)
    first_timestamp = (NOW - timedelta(minutes=61)).astimezone(timezone(timedelta(hours=2)))
    first = candle(
        0,
        timestamp=first_timestamp,
    )
    gap_after_first = candle(2)

    assert buffer.append(first, now=NOW).candles[0].timestamp.tzinfo is UTC
    gap = buffer.append(gap_after_first, now=NOW)
    assert gap.gap_reset
    assert [item.timestamp for item in gap.candles] == [NOW - timedelta(minutes=59)]
    assert NOW - timedelta(minutes=60) not in [item.timestamp for item in gap.candles]

    before_duplicate = buffer.snapshot(EPIC)
    with pytest.raises(ShadowRuntimeError, match="already emitted"):
        buffer.append(candle(2), now=NOW)
    assert buffer.snapshot(EPIC) == before_duplicate

    bounded = buffer.append(candle(3), now=NOW).candles
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


def test_atr_stop_above_broker_minimum_is_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    created = warm(runtime(), monkeypatch, "raw-atr-stop", minimum_stop_pips=1.0)

    assert created.intent is not None
    assert created.intent.stop_price == pytest.approx(0.8493)
    assert created.intent.target_price == pytest.approx(0.8513)


def test_broker_minimum_replaces_smaller_atr_stop_and_preserves_reward_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = warm(
        runtime(),
        monkeypatch,
        "broker-minimum-stop",
        atr=0.00002,
        minimum_stop_pips=2.0,
        offer=0.85002,
    )

    assert created.intent is not None
    entry = created.intent.entry_price
    risk_distance = entry - created.intent.stop_price
    reward_distance = created.intent.target_price - entry
    assert risk_distance / 0.0001 == pytest.approx(2.0)
    assert reward_distance / 0.0001 == pytest.approx(3.0)
    assert reward_distance == pytest.approx(risk_distance * 1.5)


def test_broker_minimum_above_maximum_stop_rejects_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryShadowStore(7)
    result = warm(
        runtime(store),
        monkeypatch,
        "broker-stop-too-wide",
        minimum_stop_pips=12.1,
    )

    assert result.decision_code == "MAXIMUM_STOP_EXCEEDED"
    assert result.intent is None
    assert store.records == {}


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

    def always_true_core(_risk_gate: object) -> ShadowExecutionCore:
        return ShadowExecutionCore(
            mode=ExecutionMode.SHADOW_DEMO,
            lease=value.lease,
            store=store,
            risk_gate=lambda *_args, **_kwargs: True,
            instruments=value.instruments,
        )

    monkeypatch.setattr(value, "_new_execution_core", always_true_core)

    result = warm(
        value,
        monkeypatch,
        "daily-loss-veto",
        account_value=account(balance=9_400.0),
    )

    assert result.decision_code == "DAILY_LOSS_LIMIT"
    assert result.intent is None
    assert store.records == {}


def test_one_shot_risk_permit_can_be_consumed_only_once() -> None:
    permit = OneShotRiskPermit()

    assert permit.consume()
    assert permit.consumed
    assert not permit.consume()


def test_concurrent_evaluation_permits_cannot_consume_each_other() -> None:
    permits = (OneShotRiskPermit(), OneShotRiskPermit())
    barrier = Barrier(2)

    def consume_twice(permit: OneShotRiskPermit) -> tuple[bool, bool]:
        barrier.wait()
        return permit.consume(), permit.consume()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(consume_twice, permits))

    assert results == ((True, False), (True, False))


def test_failed_create_cannot_leave_a_reusable_risk_permit() -> None:
    class FailingStore(InMemoryShadowStore):
        def put(self, record):
            raise ShadowExecutionError("test create failure")

    permit = OneShotRiskPermit()
    store = FailingStore(7)
    core = ShadowExecutionCore(
        mode=ExecutionMode.SHADOW_DEMO,
        lease=Lease(),
        store=store,
        risk_gate=permit.consume,
        instruments=InstrumentRegistry.frozen_v1(),
    )
    signal = SimpleNamespace(
        direction=SimpleNamespace(value="BUY"),
        strategy_name="S0",
        epic=EPIC,
    )

    with pytest.raises(ShadowExecutionError, match="create failure"):
        core.create_intent(
            signal,
            MarketQuote(0.8500, 0.8501, NOW),
            stop_price=0.8493,
            target_price=0.8513,
            open_positions_for_strategy=0,
            daily_loss_pct=0.0,
            now=NOW,
        )
    assert permit.consumed
    with pytest.raises(ShadowExecutionError, match="risk vetoed"):
        core.create_intent(
            signal,
            MarketQuote(0.8500, 0.8501, NOW),
            stop_price=0.8493,
            target_price=0.8513,
            open_positions_for_strategy=0,
            daily_loss_pct=0.0,
            now=NOW,
        )


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


@pytest.mark.parametrize("minimum_stop_pips", [None, float("nan"), 0.0, -1.0])
def test_invalid_broker_minimum_stop_fails_closed_without_intent(
    minimum_stop_pips: object,
) -> None:
    store = InMemoryShadowStore(7)
    value = runtime(store)
    invalid = ShadowMarketQuote(
        EPIC,
        0.8500,
        0.8501,
        NOW,
        1.0,
        0.01,
        minimum_stop_pips,
    )

    with pytest.raises(ShadowRuntimeError, match="market quote"):
        value.evaluate_cycle(
            cycle_id="invalid-minimum-stop",
            quote=invalid,
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


def test_gap_resets_warmup_without_synthetic_candles_then_allows_new_contiguous_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryShadowStore(7)
    value = runtime(store)
    strategy_calls: list[None] = []
    force_s0_signal(monkeypatch, value, calls=strategy_calls)
    start = NOW - timedelta(minutes=121)

    for offset in range(59):
        result = value.evaluate_cycle(
            cycle_id=f"pre-gap-{offset}",
            quote=quote(),
            candle=candle(0, timestamp=start + timedelta(minutes=offset)),
            account=account(),
            now=NOW,
        )
        assert result.decision_code == "WARMUP_INCOMPLETE"

    gap = value.evaluate_cycle(
        cycle_id="gap",
        quote=quote(),
        candle=candle(0, timestamp=start + timedelta(minutes=60)),
        account=account(),
        now=NOW,
    )
    assert gap.decision_code == "CANDLE_GAP_WARMUP_RESET"
    assert gap.intent is None
    assert [item.timestamp for item in value._candles.snapshot(EPIC)] == [
        start + timedelta(minutes=60)
    ]
    assert store.records == {}
    assert strategy_calls == []

    result = gap
    for offset in range(61, 120):
        result = value.evaluate_cycle(
            cycle_id="contiguous-after-gap" if offset == 119 else f"post-gap-{offset}",
            quote=quote(),
            candle=candle(0, timestamp=start + timedelta(minutes=offset)),
            account=account(),
            now=NOW,
        )
    assert result.intent is not None
    assert strategy_calls == [None]
    window = value._candles.snapshot(EPIC)
    assert len(window) == 60
    assert all(
        later.timestamp - earlier.timestamp == timedelta(minutes=1)
        for earlier, later in zip(window[:-1], window[1:], strict=True)
    )


def test_runtime_advances_created_open_closed_and_reconciled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryShadowStore(7)
    evidence = InMemoryShadowEvidenceStore()
    value = runtime(store, evidence)
    created_result = warm(value, monkeypatch, "restart-cycle")
    created = created_result.intent
    assert created is not None
    assert created_result.evidence.decision_code == "SHADOW_INTENT_CREATED"
    assert created_result.evidence.portfolio_risk_code == "ALLOWED"
    assert created_result.evidence.lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED
    assert created_result.evidence.configuration_identity == value.config.shadow_configuration_hash
    assert created_result.evidence.authorized is False
    assert created_result.evidence.order_authority is False
    assert created_result.evidence.broker_order_call_count == 0

    after_created = value.advance(created.intent_id, quote(), now=NOW)
    assert after_created.lifecycle is ShadowLifecycle.OPEN
    assert evidence.by_intent(created.intent_id).lifecycle is ShadowLifecycle.OPEN
    after_open = value.advance(
        created.intent_id,
        quote(bid=0.8514, offer=0.8515),
        now=NOW,
    )
    assert after_open.lifecycle is ShadowLifecycle.CLOSED
    assert evidence.by_intent(created.intent_id).lifecycle is ShadowLifecycle.CLOSED
    after_closed = value.advance(created.intent_id, quote(), now=NOW)
    assert after_closed.lifecycle is ShadowLifecycle.RECONCILED
    assert evidence.by_intent(created.intent_id).lifecycle is ShadowLifecycle.RECONCILED
    duplicate = warm(runtime(store, evidence), monkeypatch, "restart-cycle")
    assert duplicate.evidence.lifecycle is ShadowLifecycle.RECONCILED
    assert value.advance(created.intent_id, quote(), now=NOW) == after_closed


def test_evidence_reflects_failed_safe_lifecycle_without_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryShadowStore(7)
    evidence = InMemoryShadowEvidenceStore()
    value = runtime(store, evidence)
    created = warm(value, monkeypatch, "failed-safe-cycle").intent
    assert created is not None
    opened = value.advance(created.intent_id, quote(), now=NOW)
    failed_safe = store.transition(
        opened.intent_id,
        ShadowLifecycle.OPEN,
        ShadowLifecycle.FAILED_SAFE,
        Lease.fencing_token,
        updated_at=NOW,
    )

    assert value.advance(failed_safe.intent_id, quote(), now=NOW) == failed_safe
    persisted = evidence.by_intent(failed_safe.intent_id)
    assert persisted is not None
    assert persisted.lifecycle is ShadowLifecycle.FAILED_SAFE
    with pytest.raises(ShadowRuntimeError, match="lifecycle regressed"):
        evidence.record(replace(persisted, lifecycle=ShadowLifecycle.OPEN))
    with pytest.raises(ShadowRuntimeError, match="identity conflicts"):
        evidence.record(replace(persisted, intent_id=uuid4()))
    with pytest.raises(ShadowRuntimeError, match="identity conflicts"):
        evidence.record(replace(persisted, decision_code="CONFLICT"))


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
    evidence = InMemoryShadowEvidenceStore()
    created_runtime = ShadowRuntime(
        lease=leader,
        store=store,
        evidence=evidence,
    )
    created = warm(created_runtime, monkeypatch, "runtime-cycle").intent
    assert created is not None
    assert evidence.by_intent(created.intent_id).lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED
    assert created_runtime.authorized is False
    assert created_runtime.order_authority is False
    assert created_runtime.broker_order_call_count == 0
    assert (
        warm(
            ShadowRuntime(lease=leader, store=store, evidence=evidence),
            monkeypatch,
            "runtime-cycle",
        ).evidence.lifecycle
        is ShadowLifecycle.SHADOW_INTENT_CREATED
    )
    assert (
        warm(
            ShadowRuntime(lease=leader, store=store, evidence=evidence),
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
        evidence=evidence,
    )
    opened = restarted.advance(created.intent_id, quote(), now=NOW)
    assert opened.lifecycle is ShadowLifecycle.OPEN
    assert evidence.by_intent(created.intent_id).lifecycle is ShadowLifecycle.OPEN
    limited = warm(
        ShadowRuntime(lease=successor, store=successor_store, evidence=evidence),
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
    assert evidence.by_intent(created.intent_id).lifecycle is ShadowLifecycle.CLOSED
    assert (
        restarted.advance(created.intent_id, quote(), now=NOW).lifecycle
        is ShadowLifecycle.RECONCILED
    )
    assert evidence.by_intent(created.intent_id).lifecycle is ShadowLifecycle.RECONCILED
