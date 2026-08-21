from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.ig_trader.shadow_execution import (
    ExecutionMode,
    InMemoryShadowStore,
    MarketQuote,
    ShadowExecutionCore,
    ShadowExecutionError,
    ShadowLifecycle,
)

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


class Lease:
    authorized = True
    fencing_token = 7


def signal(direction: str = "BUY") -> SimpleNamespace:
    return SimpleNamespace(
        direction=SimpleNamespace(value=direction), strategy_name="S0", epic="EURGBP"
    )


def core(
    *, mode: ExecutionMode = ExecutionMode.SHADOW_DEMO, risk_gate=lambda *_a, **_k: True
) -> tuple[ShadowExecutionCore, InMemoryShadowStore]:
    store = InMemoryShadowStore()
    return ShadowExecutionCore(mode=mode, lease=Lease(), store=store, risk_gate=risk_gate), store


def quote(bid: float = 0.8498, offer: float = 0.8500) -> MarketQuote:
    return MarketQuote(bid, offer, NOW)


def test_shadow_mode_has_no_order_authority_and_never_receives_broker() -> None:
    execution, _store = core()
    assert execution.authorized is True
    assert execution.order_authority is False
    assert not hasattr(execution, "broker")


def test_risk_veto_blocks_malicious_signal() -> None:
    execution, store = core(risk_gate=lambda *_a, **_k: False)

    with pytest.raises(ShadowExecutionError, match="risk"):
        execution.create_intent(
            signal(),
            quote(),
            stop_price=0.8490,
            target_price=0.8510,
            open_positions_for_strategy=0,
            daily_loss_pct=0,
            now=NOW,
        )
    assert store.records == {}


def test_stale_lease_cannot_mutate_state() -> None:
    execution, store = core()
    execution.lease.authorized = False

    with pytest.raises(ShadowExecutionError):
        execution.create_intent(
            signal(),
            quote(),
            stop_price=0.8490,
            target_price=0.8510,
            open_positions_for_strategy=0,
            daily_loss_pct=0,
            now=NOW,
        )
    assert store.records == {}


def test_duplicate_intent_is_idempotent() -> None:
    execution, store = core()
    intent_id = uuid4()
    first = execution.create_intent(
        signal(),
        quote(),
        intent_id=intent_id,
        stop_price=0.8490,
        target_price=0.8510,
        open_positions_for_strategy=0,
        daily_loss_pct=0,
        now=NOW,
    )
    second = execution.create_intent(
        signal(),
        quote(),
        intent_id=intent_id,
        stop_price=0.8490,
        target_price=0.8510,
        open_positions_for_strategy=0,
        daily_loss_pct=0,
        now=NOW,
    )
    assert first == second
    assert len(store.records) == 1


def test_long_entry_uses_offer_and_target_exit_uses_bid() -> None:
    execution, _store = core()
    position = execution.create_intent(
        signal("BUY"),
        quote(),
        stop_price=0.8490,
        target_price=0.8510,
        open_positions_for_strategy=0,
        daily_loss_pct=0,
        now=NOW,
    )
    assert position.entry_price == 0.8500
    closed = execution.advance(position, quote(bid=0.8510, offer=0.8512), now=NOW)
    assert closed.lifecycle is ShadowLifecycle.RECONCILED
    assert closed.exit_price == 0.8510


def test_short_entry_uses_bid_and_stop_exit_uses_offer() -> None:
    execution, _store = core()
    position = execution.create_intent(
        signal("SELL"),
        quote(),
        stop_price=0.8510,
        target_price=0.8490,
        open_positions_for_strategy=0,
        daily_loss_pct=0,
        now=NOW,
    )
    assert position.entry_price == 0.8498
    closed = execution.advance(position, quote(bid=0.8509, offer=0.8510), now=NOW)
    assert closed.lifecycle is ShadowLifecycle.RECONCILED
    assert closed.exit_price == 0.8510


def test_missing_or_crossed_market_is_no_trade() -> None:
    execution, store = core()
    for market in (
        MarketQuote(0, 1, NOW),
        MarketQuote(1, 0, NOW),
        MarketQuote(1, 2, NOW - timedelta(minutes=1)),
    ):
        with pytest.raises(ShadowExecutionError):
            execution.create_intent(
                signal(),
                market,
                stop_price=0.8490,
                target_price=0.8510,
                open_positions_for_strategy=0,
                daily_loss_pct=0,
                now=NOW,
            )
    assert store.records == {}


def test_risk_exception_fails_closed() -> None:
    execution, store = core(risk_gate=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError()))

    with pytest.raises(ShadowExecutionError, match="failed closed"):
        execution.create_intent(
            signal(),
            quote(),
            stop_price=0.8490,
            target_price=0.8510,
            open_positions_for_strategy=0,
            daily_loss_pct=0,
            now=NOW,
        )
    assert store.records == {}


@pytest.mark.parametrize(
    "mode", [ExecutionMode.NO_EXECUTION, ExecutionMode.DEMO_EXECUTION, ExecutionMode.LIVE_EXECUTION]
)
def test_other_modes_cannot_enable_shadow_authority(mode: ExecutionMode) -> None:
    execution, store = core(mode=mode)
    with pytest.raises(ShadowExecutionError):
        execution.create_intent(
            signal(),
            quote(),
            stop_price=0.8490,
            target_price=0.8510,
            open_positions_for_strategy=0,
            daily_loss_pct=0,
            now=NOW,
        )
    assert execution.order_authority is False
    assert store.records == {}
