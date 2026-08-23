from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

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
    ShadowCycleEvidence,
    ShadowMarketQuote,
    ShadowRuntime,
    ShadowRuntimeError,
    derive_global_cycle_id,
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
    transform: Callable[[Signal], object] | None = None,
) -> None:
    def generate(epic: str, frame: object) -> Signal:
        if calls is not None:
            calls.append(None)
        timestamp = frame.index[-1]  # type: ignore[union-attr]
        signal = Signal(
            epic,
            SignalDirection.BUY,
            timestamp,
            0.8500,
            "Scalper",
            0.9,
            {"atr": atr},
        )
        return transform(signal) if transform is not None else signal

    monkeypatch.setattr(value.strategy, "generate_signal", generate)


def warm(
    runtime_value: ShadowRuntime,
    monkeypatch: pytest.MonkeyPatch,
    *,
    epic: str = EPIC,
    account_value: object | None = None,
    minimum_stop_pips: float = 1.0,
    atr: float = 0.0004,
    bid: float = 0.8500,
    offer: float = 0.8501,
    minimum_size: float = 0.01,
    now: datetime = NOW,
    latest_candle_at: datetime | None = None,
    calls: list[None] | None = None,
    signal_transform: Callable[[Signal], object] | None = None,
):
    force_s0_signal(
        monkeypatch,
        runtime_value,
        atr=atr,
        calls=calls,
        transform=signal_transform,
    )
    result = None
    finalized_at = latest_candle_at or now - timedelta(minutes=2)
    for index in range(60):
        result = runtime_value.evaluate_cycle(
            quote=(
                quote(
                    epic=epic,
                    bid=bid,
                    offer=offer,
                    minimum_stop_pips=minimum_stop_pips,
                    minimum_size=minimum_size,
                    timestamp=now,
                )
            ),
            candle=candle(
                index,
                epic=epic,
                timestamp=finalized_at - timedelta(minutes=59 - index),
            ),
            account=account(timestamp=now) if account_value is None else account_value,  # type: ignore[arg-type]
            now=now,
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
    created = warm(runtime(store), monkeypatch)
    assert created.intent is not None
    assert created.intent.lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED
    assert created.intent.stop_price == pytest.approx(0.8493)
    assert created.intent.target_price == pytest.approx(0.8513)

    restarted = warm(runtime(store), monkeypatch)
    assert restarted.decision_code == "DUPLICATE_CYCLE"
    assert restarted.intent == created.intent
    assert store.active_position_count() == 1


def test_atr_stop_above_broker_minimum_is_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    created = warm(runtime(), monkeypatch, minimum_stop_pips=1.0)

    assert created.intent is not None
    assert created.intent.stop_price == pytest.approx(0.8493)
    assert created.intent.target_price == pytest.approx(0.8513)


def test_broker_minimum_replaces_smaller_atr_stop_and_preserves_reward_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = warm(
        runtime(),
        monkeypatch,
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
        minimum_stop_pips=12.1,
    )

    assert result.decision_code == "MAXIMUM_STOP_EXCEEDED"
    assert result.intent is None
    assert store.records == {}


def test_conflicting_global_cycle_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryShadowStore(7)
    created = warm(runtime(store), monkeypatch)
    assert created.intent is not None
    duplicate = warm(runtime(store), monkeypatch)
    assert duplicate.decision_code == "DUPLICATE_CYCLE"
    assert duplicate.intent == created.intent

    for conflicting_epic in (EURUSD_EPIC, GBPUSD_EPIC):
        with pytest.raises(ShadowExecutionError, match="duplicate"):
            warm(runtime(store), monkeypatch, epic=conflicting_epic)
    assert store.get(created.intent.intent_id) == created.intent
    assert len(store.records) == 1


def test_global_cycle_identity_is_internal_and_instrument_neutral() -> None:
    timestamp = NOW - timedelta(minutes=2)
    configuration = FrozenV1Config().shadow_configuration_hash

    cycle_ids = {
        derive_global_cycle_id(timestamp, configuration)
        for _epic in (EPIC, EURUSD_EPIC, GBPUSD_EPIC)
    }

    assert len(cycle_ids) == 1
    assert derive_global_cycle_id(timestamp + timedelta(minutes=1), configuration) not in cycle_ids
    assert derive_global_cycle_id(timestamp, f"changed-{configuration}") not in cycle_ids


def test_caller_cycle_text_cannot_bypass_internal_global_identity() -> None:
    value = runtime()

    with pytest.raises(ShadowRuntimeError, match="caller cycle identity"):
        value.evaluate_cycle(
            cycle_id="caller-selected-cycle",
            quote=quote(),
            candle=candle(0),
            account=account(),
            now=NOW,
        )

    assert value._candles.snapshot(EPIC) == ()


def test_concurrent_same_global_cycle_leaves_one_durable_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryShadowStore(7)
    evidence = InMemoryShadowEvidenceStore()
    values = (runtime(store, evidence), runtime(store, evidence))
    for value in values:
        force_s0_signal(monkeypatch, value)

    def evaluate(value: ShadowRuntime):
        result = None
        for index in range(60):
            result = value.evaluate_cycle(
                quote=quote(),
                candle=candle(index),
                account=account(),
                now=NOW,
            )
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(evaluate, values))

    assert all(result is not None and result.intent is not None for result in results)
    assert len(store.records) == 1
    assert results[0].cycle_id == results[1].cycle_id
    assert results[0].intent.intent_id == results[1].intent.intent_id


@pytest.mark.parametrize(
    "transform",
    [
        lambda signal: replace(signal, epic=EURUSD_EPIC),
        lambda signal: replace(signal, timestamp=signal.timestamp - timedelta(minutes=1)),
        lambda signal: replace(
            signal, timestamp=signal.timestamp.to_pydatetime().replace(tzinfo=None)
        ),
        lambda signal: replace(signal, strategy_name="not-scalper"),
        lambda signal: replace(signal, price=float("nan")),
        lambda signal: replace(signal, price=-1.0),
        lambda signal: replace(signal, confidence=0.69),
        lambda signal: replace(signal, confidence=1.01),
        lambda signal: replace(signal, metadata={}),
        lambda signal: replace(signal, metadata={"atr": float("nan")}),
    ],
    ids=[
        "epic",
        "timestamp",
        "naive-timestamp",
        "strategy",
        "nan-price",
        "negative-price",
        "low-actionable-confidence",
        "confidence-above-one",
        "missing-atr",
        "invalid-atr",
    ],
)
def test_invalid_strategy_output_fails_closed_before_risk_permit(
    monkeypatch: pytest.MonkeyPatch,
    transform: Callable[[Signal], object],
) -> None:
    store = InMemoryShadowStore(7)
    evidence = InMemoryShadowEvidenceStore()
    value = runtime(store, evidence)
    core_calls: list[None] = []

    def count_core(_risk_gate: object) -> ShadowExecutionCore:
        core_calls.append(None)
        raise AssertionError("invalid strategy output reached execution core")

    monkeypatch.setattr(value, "_new_execution_core", count_core)
    result = warm(value, monkeypatch, signal_transform=transform)

    assert result.decision_code == "STRATEGY_OUTPUT_INVALID"
    assert result.intent is None
    assert result.evidence.decision_code == "STRATEGY_OUTPUT_INVALID"
    assert core_calls == []
    assert store.records == {}


def test_valid_wait_output_is_recorded_without_an_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    result = warm(
        runtime(),
        monkeypatch,
        signal_transform=lambda signal: replace(
            signal,
            direction=SignalDirection.WAIT,
            confidence=0.0,
            metadata={},
        ),
    )

    assert result.decision_code == "S0_WAIT"
    assert result.intent is None


@pytest.mark.parametrize("direction", [SignalDirection.BUY, SignalDirection.SELL])
def test_valid_actionable_strategy_outputs_can_create_shadow_intents(
    monkeypatch: pytest.MonkeyPatch,
    direction: SignalDirection,
) -> None:
    result = warm(
        runtime(),
        monkeypatch,
        signal_transform=lambda signal: replace(signal, direction=direction),
    )

    assert result.intent is not None
    assert result.intent.direction == direction.value


def test_completed_candle_window_accepts_current_and_safe_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = warm(runtime(), monkeypatch)
    boundary_now = NOW + timedelta(seconds=30)
    at_safe_boundary = warm(
        runtime(),
        monkeypatch,
        now=boundary_now,
        latest_candle_at=NOW - timedelta(minutes=2),
    )

    assert current.intent is not None
    assert at_safe_boundary.intent is not None


@pytest.mark.parametrize(
    "latest_candle_at",
    [NOW - timedelta(minutes=3), NOW - timedelta(minutes=1)],
    ids=["stale", "not-completed"],
)
def test_stale_or_unfinished_candle_window_does_not_run_s0_or_create_intent(
    monkeypatch: pytest.MonkeyPatch,
    latest_candle_at: datetime,
) -> None:
    store = InMemoryShadowStore(7)
    calls: list[None] = []
    result = warm(
        runtime(store),
        monkeypatch,
        latest_candle_at=latest_candle_at,
        calls=calls,
    )

    assert result.decision_code == "CANDLE_WINDOW_STALE"
    assert result.intent is None
    assert calls == []
    assert store.records == {}


def test_risk_evidence_captures_allowed_and_vetoed_candidate_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = warm(runtime(), monkeypatch).evidence.risk
    daily_loss = warm(
        runtime(),
        monkeypatch,
        account_value=account(balance=9_400.0),
    ).evidence.risk
    minimum_size = warm(runtime(), monkeypatch, minimum_size=2.0).evidence.risk
    maximum_stop = warm(runtime(), monkeypatch, minimum_stop_pips=12.1).evidence.risk
    maximum_spread = warm(runtime(), monkeypatch, offer=0.85013).evidence.risk
    spread_target = warm(
        runtime(),
        monkeypatch,
        atr=0.00002,
        offer=0.85003,
    ).evidence.risk
    position_store = InMemoryShadowStore(7)
    assert warm(runtime(position_store), monkeypatch).intent is not None
    total_position = warm(
        runtime(position_store),
        monkeypatch,
        now=NOW + timedelta(minutes=1),
    ).evidence.risk

    assert allowed is not None and allowed.allowed is True
    assert allowed.current_positions == 0 and allowed.projected_positions == 1
    assert allowed.effective_stop_pips == pytest.approx(8.0)
    assert allowed.target_pips == pytest.approx(12.0)
    assert allowed.hypothetical_size is not None and allowed.monetary_risk is not None
    assert daily_loss is not None and daily_loss.code == "DAILY_LOSS_LIMIT"
    assert daily_loss.daily_loss_pct == pytest.approx(-0.06)
    assert minimum_size is not None and minimum_size.code == "POSITION_SIZE_BELOW_MINIMUM"
    assert maximum_stop is not None and maximum_stop.effective_stop_pips == pytest.approx(12.1)
    assert maximum_spread is not None and maximum_spread.spread_pips == pytest.approx(1.3)
    assert spread_target is not None and spread_target.spread_to_target_ratio == pytest.approx(0.2)
    assert total_position is not None and total_position.code == "TOTAL_POSITION_LIMIT"
    assert total_position.current_positions == 1 and total_position.projected_positions == 2


def test_evidence_rejects_conflicting_risk_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = InMemoryShadowEvidenceStore()
    created = warm(runtime(evidence=evidence), monkeypatch)
    risk = created.evidence.risk
    assert risk is not None

    with pytest.raises(ShadowRuntimeError, match="identity conflicts"):
        evidence.record(
            replace(
                created.evidence,
                risk=replace(risk, effective_stop_pips=risk.effective_stop_pips + 1.0),
            )
        )


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
    active = warm(runtime(store), monkeypatch)
    assert active.intent is not None

    result = warm(runtime(store), monkeypatch, now=NOW + timedelta(minutes=1))

    assert result.decision_code == "TOTAL_POSITION_LIMIT"
    assert result.intent is None
    assert len(store.records) == 1


def test_portfolio_risk_missing_account_state_vetoes(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryShadowStore(7)

    result = warm(runtime(store), monkeypatch, account_value=object())

    assert result.decision_code == "ACCOUNT_STATE_UNKNOWN"
    assert result.intent is None
    assert store.records == {}


def test_missing_instrument_metadata_fails_closed_without_intent() -> None:
    store = InMemoryShadowStore(7)
    value = runtime(store)
    unsupported = "UNSUPPORTED"

    with pytest.raises(ShadowExecutionError, match="instrument"):
        value.evaluate_cycle(
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
            quote=quote(),
            candle=candle(0, timestamp=start + timedelta(minutes=offset)),
            account=account(),
            now=NOW,
        )
        assert result.decision_code == "WARMUP_INCOMPLETE"

    gap = value.evaluate_cycle(
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
    created_result = warm(value, monkeypatch)
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
    closed_evidence = evidence.by_intent(created.intent_id)
    assert closed_evidence.lifecycle is ShadowLifecycle.CLOSED
    assert closed_evidence.performance is not None
    assert closed_evidence.performance.exit_reason == "TARGET"
    assert "cash" not in asdict(closed_evidence.performance)
    after_closed = value.advance(created.intent_id, quote(), now=NOW)
    assert after_closed.lifecycle is ShadowLifecycle.RECONCILED
    reconciled_evidence = evidence.by_intent(created.intent_id)
    assert reconciled_evidence.lifecycle is ShadowLifecycle.RECONCILED
    assert reconciled_evidence.performance == closed_evidence.performance
    duplicate = warm(runtime(store, evidence), monkeypatch)
    assert duplicate.evidence.lifecycle is ShadowLifecycle.RECONCILED
    assert value.advance(created.intent_id, quote(), now=NOW) == after_closed


def test_evidence_reflects_failed_safe_lifecycle_without_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryShadowStore(7)
    evidence = InMemoryShadowEvidenceStore()
    value = runtime(store, evidence)
    created = warm(value, monkeypatch).intent
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
    assert persisted.performance is None
    with pytest.raises(ShadowRuntimeError, match="lifecycle regressed"):
        evidence.record(replace(persisted, lifecycle=ShadowLifecycle.OPEN))
    with pytest.raises(ShadowRuntimeError, match="identity conflicts"):
        evidence.record(replace(persisted, intent_id=uuid4()))
    with pytest.raises(ShadowRuntimeError, match="identity conflicts"):
        evidence.record(replace(persisted, decision_code="CONFLICT"))


@pytest.mark.parametrize(
    ("direction", "exit_quote", "reason"),
    [
        (SignalDirection.BUY, quote(bid=0.8492, offer=0.8493), "STOP"),
        (SignalDirection.BUY, quote(bid=0.8514, offer=0.8515), "TARGET"),
        (SignalDirection.SELL, quote(bid=0.8508, offer=0.8509), "STOP"),
        (SignalDirection.SELL, quote(bid=0.8486, offer=0.8487), "TARGET"),
    ],
    ids=["buy-stop", "buy-target", "sell-stop", "sell-target"],
)
def test_closed_performance_evidence_is_complete_and_reconciliation_retains_it(
    monkeypatch: pytest.MonkeyPatch,
    direction: SignalDirection,
    exit_quote: ShadowMarketQuote,
    reason: str,
) -> None:
    store = InMemoryShadowStore(7)
    evidence = InMemoryShadowEvidenceStore()
    value = runtime(store, evidence)
    created = warm(
        value,
        monkeypatch,
        signal_transform=lambda signal: replace(signal, direction=direction),
    ).intent
    assert created is not None
    assert value.advance(created.intent_id, quote(), now=NOW).lifecycle is ShadowLifecycle.OPEN
    closed = value.advance(created.intent_id, exit_quote, now=NOW)
    closed_evidence = evidence.by_intent(created.intent_id)

    assert closed.lifecycle is ShadowLifecycle.CLOSED
    assert closed_evidence is not None and closed_evidence.performance is not None
    assert closed_evidence.performance.direction == direction.value
    assert closed_evidence.performance.exit_reason == reason
    assert closed_evidence.performance.entry_price == created.entry_price
    assert closed_evidence.performance.exit_price == closed.exit_price
    assert "cash" not in asdict(closed_evidence.performance)
    assert (
        value.advance(created.intent_id, quote(), now=NOW).lifecycle is ShadowLifecycle.RECONCILED
    )
    assert evidence.by_intent(created.intent_id).performance == closed_evidence.performance


def test_evidence_rejects_performance_disappearance_or_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryShadowStore(7)
    evidence = InMemoryShadowEvidenceStore()
    value = runtime(store, evidence)
    created = warm(value, monkeypatch).intent
    assert created is not None
    value.advance(created.intent_id, quote(), now=NOW)
    value.advance(created.intent_id, quote(bid=0.8514, offer=0.8515), now=NOW)
    closed_evidence = evidence.by_intent(created.intent_id)
    assert closed_evidence is not None and closed_evidence.performance is not None

    with pytest.raises(ShadowRuntimeError, match="performance"):
        evidence.record(replace(closed_evidence, performance=None))
    with pytest.raises(ShadowRuntimeError, match="performance conflicts"):
        evidence.record(
            replace(
                closed_evidence,
                performance=replace(closed_evidence.performance, pips=999.0),
            )
        )


def test_missing_evidence_blocks_lifecycle_mutation_before_store_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryShadowStore(7)
    created = warm(runtime(store), monkeypatch).intent
    assert created is not None

    with pytest.raises(ShadowRuntimeError, match="EVIDENCE_STATE_MISSING"):
        runtime(store, InMemoryShadowEvidenceStore()).advance(created.intent_id, quote(), now=NOW)

    assert store.get(created.intent_id).lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED


def test_evidence_write_failure_is_visible_and_blocks_lifecycle_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingEvidence(InMemoryShadowEvidenceStore):
        fail_writes = False

        def record(self, evidence: ShadowCycleEvidence) -> ShadowCycleEvidence:
            if self.fail_writes:
                raise ShadowRuntimeError("evidence write failed")
            return super().record(evidence)

    store = InMemoryShadowStore(7)
    evidence = FailingEvidence()
    value = runtime(store, evidence)
    created = warm(value, monkeypatch).intent
    assert created is not None
    evidence.fail_writes = True

    with pytest.raises(ShadowRuntimeError, match="evidence write failed"):
        value.advance(created.intent_id, quote(), now=NOW)

    assert store.get(created.intent_id).lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED
    assert evidence.by_intent(created.intent_id).lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED


@pytest.mark.parametrize("count", [False, 1.5, -1], ids=["boolean", "non-integer", "negative"])
def test_ambiguous_active_position_count_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    count: object,
) -> None:
    class AmbiguousStore(InMemoryShadowStore):
        def active_position_count(self) -> object:
            return count

    store = AmbiguousStore(7)

    with pytest.raises(ShadowRuntimeError, match="active-position state is ambiguous"):
        warm(runtime(store), monkeypatch)

    assert store.records == {}


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
    created = warm(created_runtime, monkeypatch).intent
    assert created is not None
    assert evidence.by_intent(created.intent_id).lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED
    expected_cycle = derive_global_cycle_id(
        NOW - timedelta(minutes=2),
        SHADOW_CONFIGURATION_HASH,
    )
    assert created.intent_id == uuid5(
        NAMESPACE_URL,
        f"shadow-runtime:{expected_cycle}:{SHADOW_CONFIGURATION_HASH}",
    )
    assert created_runtime.authorized is False
    assert created_runtime.order_authority is False
    assert created_runtime.broker_order_call_count == 0
    with pytest.raises(ShadowRuntimeError, match="EVIDENCE_STATE_MISSING"):
        ShadowRuntime(
            lease=leader,
            store=store,
            evidence=InMemoryShadowEvidenceStore(),
        ).advance(created.intent_id, quote(), now=NOW)
    assert store.get(created.intent_id).lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED
    assert (
        warm(
            ShadowRuntime(lease=leader, store=store, evidence=evidence),
            monkeypatch,
        ).evidence.lifecycle
        is ShadowLifecycle.SHADOW_INTENT_CREATED
    )
    assert (
        warm(
            ShadowRuntime(lease=leader, store=store, evidence=evidence),
            monkeypatch,
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
        now=NOW + timedelta(minutes=1),
    )
    assert limited.intent is None and limited.decision_code == "TOTAL_POSITION_LIMIT"
    closed = restarted.advance(
        created.intent_id,
        quote(bid=0.8514, offer=0.8515),
        now=NOW,
    )
    assert closed.lifecycle is ShadowLifecycle.CLOSED
    assert evidence.by_intent(created.intent_id).lifecycle is ShadowLifecycle.CLOSED
    assert evidence.by_intent(created.intent_id).performance is not None
    closed_performance = evidence.by_intent(created.intent_id).performance
    assert (
        restarted.advance(created.intent_id, quote(), now=NOW).lifecycle
        is ShadowLifecycle.RECONCILED
    )
    assert evidence.by_intent(created.intent_id).lifecycle is ShadowLifecycle.RECONCILED
    assert evidence.by_intent(created.intent_id).performance == closed_performance
