from datetime import UTC, datetime, timedelta
from math import inf, nan
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.ig_trader.shadow_execution import (
    ExecutionMode,
    InMemoryShadowStore,
    InstrumentRegistry,
    MarketQuote,
    ShadowExecutionCore,
    ShadowExecutionError,
    ShadowLifecycle,
)

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
EPIC = "CS.D.EURGBP.MINI.IP"


class Lease:
    authorized = True
    fencing_token = 7


def signal(direction: str = "BUY", epic: str = EPIC) -> SimpleNamespace:
    return SimpleNamespace(
        direction=SimpleNamespace(value=direction), strategy_name="S0", epic=epic
    )


def core(
    *,
    mode: ExecutionMode = ExecutionMode.SHADOW_DEMO,
    risk_gate=lambda *_a, **_k: True,
) -> tuple[ShadowExecutionCore, InMemoryShadowStore]:
    lease = Lease()
    store = InMemoryShadowStore(lease.fencing_token)
    return (
        ShadowExecutionCore(
            mode=mode,
            lease=lease,
            store=store,
            risk_gate=risk_gate,
            instruments=InstrumentRegistry.frozen_v1(),
        ),
        store,
    )


def quote(bid: float = 0.8498, offer: float = 0.8500, as_of: datetime = NOW) -> MarketQuote:
    return MarketQuote(bid, offer, as_of)


def create(execution: ShadowExecutionCore, **overrides: object):
    values = {
        "signal": signal(),
        "quote": quote(),
        "stop_price": 0.8490,
        "target_price": 0.8510,
        "open_positions_for_strategy": 0,
        "daily_loss_pct": 0,
        "now": NOW,
    }
    values.update(overrides)
    return execution.create_intent(**values)


def test_shadow_mode_never_reports_broker_authority() -> None:
    execution, _store = core()
    assert execution.authorized is False
    assert execution.order_authority is False
    assert execution.can_advance_shadow is True
    assert not hasattr(execution, "broker")


def test_risk_veto_and_exception_fail_closed_without_state() -> None:
    for risk_gate in (
        lambda *_a, **_k: False,
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError()),
    ):
        execution, store = core(risk_gate=risk_gate)
        with pytest.raises(ShadowExecutionError):
            create(execution)
        assert store.records == {}


def test_duplicate_identical_is_idempotent_and_conflict_fails_closed() -> None:
    execution, store = core()
    intent_id = uuid4()
    first = create(execution, intent_id=intent_id)
    assert create(execution, intent_id=intent_id) == first
    with pytest.raises(ShadowExecutionError, match="duplicate"):
        create(execution, intent_id=intent_id, target_price=0.8520)
    assert store.get(intent_id) == first


def test_true_stale_fencing_token_cannot_change_state() -> None:
    execution, store = core()
    intent = create(execution)
    opened = execution.open_intent(intent, now=NOW)
    store.set_current_fencing_token(8)

    with pytest.raises(ShadowExecutionError, match="stale|conflicting"):
        execution.close_on_quote(opened, quote(bid=0.8510, offer=0.8512), now=NOW)
    assert store.get(opened.intent_id) == opened


def test_in_memory_active_position_count_is_conservative() -> None:
    execution, store = core()
    assert store.active_position_count() == 0
    intent = create(execution)
    assert store.active_position_count() == 1
    opened = execution.open_intent(intent, now=NOW)
    assert store.active_position_count() == 1
    execution.close_on_quote(opened, quote(bid=0.8510, offer=0.8512), now=NOW)
    assert store.active_position_count() == 0


@pytest.mark.parametrize(
    "market",
    [
        quote(nan, 1),
        quote(1, inf),
        quote(-1, 1),
        quote(1, -1),
        quote(1, 0.9),
        quote(as_of=NOW + timedelta(seconds=1)),
        quote(as_of=NOW - timedelta(seconds=11)),
    ],
)
def test_invalid_quote_is_no_trade(market: MarketQuote) -> None:
    execution, store = core()
    with pytest.raises(ShadowExecutionError):
        create(execution, quote=market)
    assert store.records == {}


@pytest.mark.parametrize(
    ("direction", "stop", "target"),
    [
        ("BUY", 0.8500, 0.8510),
        ("BUY", 0.8490, 0.8500),
        ("SELL", 0.8498, 0.8490),
        ("SELL", 0.8490, 0.8500),
        ("BUY", nan, 0.8510),
        ("SELL", 0.8510, inf),
    ],
)
def test_invalid_stop_entry_target_geometry_fails_closed(
    direction: str, stop: float, target: float
) -> None:
    execution, store = core()
    with pytest.raises(ShadowExecutionError):
        create(execution, signal=signal(direction), stop_price=stop, target_price=target)
    assert store.records == {}


def test_frozen_instrument_registry_rejects_unsupported_epic() -> None:
    execution, store = core()
    with pytest.raises(ShadowExecutionError, match="instrument"):
        create(execution, signal=signal(epic="UNSUPPORTED"))
    assert store.records == {}


def test_lifecycle_states_are_observable_and_restart_idempotent() -> None:
    execution, store = core()
    intent = create(execution)
    assert intent.lifecycle is ShadowLifecycle.SHADOW_INTENT_CREATED
    restarted = ShadowExecutionCore(
        mode=ExecutionMode.SHADOW_DEMO,
        lease=execution.lease,
        store=store,
        risk_gate=lambda *_a, **_k: True,
        instruments=InstrumentRegistry.frozen_v1(),
    )
    opened = restarted.open_intent(store.get(intent.intent_id), now=NOW)
    assert opened.lifecycle is ShadowLifecycle.OPEN
    assert restarted.open_intent(opened, now=NOW) == opened
    closed = restarted.close_on_quote(opened, quote(bid=0.8510, offer=0.8512), now=NOW)
    assert closed.lifecycle is ShadowLifecycle.CLOSED
    assert store.get(intent.intent_id) == closed
    reconciled = restarted.reconcile(closed, now=NOW)
    assert reconciled.lifecycle is ShadowLifecycle.RECONCILED
    assert restarted.reconcile(reconciled, now=NOW) == reconciled


@pytest.mark.parametrize(
    ("direction", "exit_quote", "expected_exit", "expected_pips"),
    [
        ("BUY", quote(bid=0.8510, offer=0.8512), 0.8510, 10.0),
        ("SELL", quote(bid=0.8488, offer=0.8490), 0.8490, 8.0),
    ],
)
def test_performance_uses_conservative_bid_offer_without_cash_pnl(
    direction: str,
    exit_quote: MarketQuote,
    expected_exit: float,
    expected_pips: float,
) -> None:
    execution, _store = core()
    stop, target = (0.8490, 0.8510) if direction == "BUY" else (0.8510, 0.8490)
    intent = create(
        execution,
        signal=signal(direction),
        stop_price=stop,
        target_price=target,
    )
    opened = execution.open_intent(intent, now=NOW)
    closed = execution.close_on_quote(opened, exit_quote, now=NOW)
    performance = execution.performance(closed)
    assert performance.exit_price == expected_exit
    assert performance.pips == pytest.approx(expected_pips)
    assert performance.r_multiple > 0
    assert "cash" not in performance.__dataclass_fields__


@pytest.mark.parametrize(
    "mode",
    [ExecutionMode.NO_EXECUTION, ExecutionMode.DEMO_EXECUTION, ExecutionMode.LIVE_EXECUTION],
)
def test_non_shadow_modes_cannot_advance(mode: ExecutionMode) -> None:
    execution, store = core(mode=mode)
    with pytest.raises(ShadowExecutionError):
        create(execution)
    assert execution.authorized is False
    assert execution.order_authority is False
    assert store.records == {}
