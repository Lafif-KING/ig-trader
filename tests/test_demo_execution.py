"""Offline-only tests for the DQ-01 Demo dealing domain."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from src.ig_trader.demo_execution import (
    APPROVED_DEMO_EPICS,
    DEMO_ENVIRONMENT,
    DemoAuthorityGate,
    DemoConfirmation,
    DemoConfirmationStatus,
    DemoDirection,
    DemoExecutionCore,
    DemoExecutionError,
    DemoExecutionLifecycle,
    DemoExecutionMode,
    DemoExecutionRequest,
    DemoMarketMetadata,
    DemoOrderType,
    DemoPosition,
    DemoRiskApproval,
    FakeIGDemoTransport,
    InMemoryDemoExecutionStore,
    KillSwitchState,
    deterministic_deal_reference,
)
from src.ig_trader.execution import ExecutionEngine, LegacyExecutionDisabled

NOW = datetime(2026, 8, 23, 14, 34, 36, tzinfo=UTC)
EPIC = "CS.D.EURGBP.MINI.IP"
CONFIGURATION_ID = "frozen-v1-demo-qualification"


def request(
    *,
    intent_id: UUID | None = None,
    cycle_id: UUID | None = None,
    epic: str = EPIC,
    direction: DemoDirection = DemoDirection.BUY,
    size: Decimal = Decimal("1.23"),
    currency: str = "GBP",
    fencing_token: int = 7,
    stop_distance: Decimal | None = Decimal("2.0"),
    stop_level: Decimal | None = None,
    limit_distance: Decimal | None = Decimal("3.0"),
    limit_level: Decimal | None = None,
) -> DemoExecutionRequest:
    resolved_intent = intent_id or uuid4()
    resolved_cycle = cycle_id or uuid4()
    return DemoExecutionRequest(
        intent_id=resolved_intent,
        global_cycle_id=resolved_cycle,
        epic=epic,
        direction=direction,
        size=size,
        currency_code=currency,
        expiry="DFB",
        order_type=DemoOrderType.MARKET,
        force_open=True,
        guaranteed_stop=False,
        stop_distance=stop_distance,
        stop_level=stop_level,
        limit_distance=limit_distance,
        limit_level=limit_level,
        deal_reference=deterministic_deal_reference(
            intent_id=resolved_intent,
            global_cycle_id=resolved_cycle,
            epic=epic,
            configuration_identity=CONFIGURATION_ID,
        ),
        configuration_identity=CONFIGURATION_ID,
        risk_approval=DemoRiskApproval(CONFIGURATION_ID, True, NOW),
        fencing_token=fencing_token,
        created_at=NOW,
    )


def metadata(
    *,
    epic: str = EPIC,
    currency: str | None = "GBP",
    observed_at: datetime | None = NOW,
    **overrides: object,
) -> DemoMarketMetadata:
    values: dict[str, object] = {
        "epic": epic,
        "instrument_currency": currency,
        "expiry": "DFB",
        "pip_scale": Decimal("0.00001"),
        "decimal_places": 2,
        "minimum_deal_size": Decimal("1.00"),
        "minimum_stop_distance": Decimal("1.0"),
        "guaranteed_stop_supported": False,
        "market_status": "TRADEABLE",
        "observed_at": observed_at,
    }
    values.update(overrides)
    return DemoMarketMetadata(**values)  # type: ignore[arg-type]


def authority(**overrides: object) -> DemoAuthorityGate:
    values: dict[str, object] = {
        "execution_mode": DemoExecutionMode.DEMO_EXECUTION,
        "demo_order_authority": True,
        "environment": DEMO_ENVIRONMENT,
        "expected_demo_account_id": "DEMO-ACCOUNT",
        "authenticated_account_id": "DEMO-ACCOUNT",
        "lease_valid": True,
        "current_fencing_token": 7,
        "global_position_count": 0,
        "global_position_limit": 1,
        "approved_epics": APPROVED_DEMO_EPICS,
        "kill_switch_state": KillSwitchState.RELEASED,
    }
    values.update(overrides)
    return DemoAuthorityGate(**values)  # type: ignore[arg-type]


def core() -> tuple[DemoExecutionCore, FakeIGDemoTransport, InMemoryDemoExecutionStore]:
    transport = FakeIGDemoTransport()
    store = InMemoryDemoExecutionStore()
    return DemoExecutionCore(transport=transport, store=store), transport, store


def confirmation(
    item: DemoExecutionRequest,
    *,
    deal_id: str = "D-1",
    status: DemoConfirmationStatus = DemoConfirmationStatus.ACCEPTED,
    **overrides: object,
) -> DemoConfirmation:
    values: dict[str, object] = {
        "deal_reference": item.deal_reference,
        "deal_id": deal_id,
        "deal_status": status,
        "status": "OPEN" if status is DemoConfirmationStatus.ACCEPTED else "REJECTED",
        "epic": item.epic,
        "direction": item.direction,
        "size": item.size,
        "level": Decimal("0.85000"),
        "stop_level": Decimal("0.84900"),
        "limit_level": Decimal("0.85100"),
    }
    values.update(overrides)
    return DemoConfirmation(**values)  # type: ignore[arg-type]


def open_record(
    execution: DemoExecutionCore,
    transport: FakeIGDemoTransport,
    item: DemoExecutionRequest,
) -> tuple[object, DemoPosition]:
    submitted = execution.submit(item, metadata(), authority(), now=NOW)
    assert submitted.lifecycle is DemoExecutionLifecycle.SUBMITTED
    accepted = confirmation(item)
    position = DemoPosition("D-1", item.epic, item.direction, item.size)
    transport.queue_confirmation(item.deal_reference, accepted)
    transport.queue_positions((position,))
    opened = execution.reconcile_open(item.intent_id)
    assert opened.lifecycle is DemoExecutionLifecycle.OPEN_RECONCILED
    return opened, position


class NoNetworkSession:
    def __init__(self) -> None:
        self.calls = 0

    def authorized_request(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1
        raise AssertionError("legacy engine must not access its session")


def test_legacy_execution_engine_is_a_zero_http_fail_closed_stub() -> None:
    session = NoNetworkSession()
    engine = ExecutionEngine(session)

    with pytest.raises(LegacyExecutionDisabled, match="legacy ExecutionEngine is disabled"):
        engine.execute_trade(object(), 1.0, 2, 3)

    assert session.calls == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"execution_mode": DemoExecutionMode.NO_EXECUTION},
        {"execution_mode": DemoExecutionMode.SHADOW_DEMO},
        {"demo_order_authority": False},
        {"environment": "IG_LIVE"},
        {"expected_demo_account_id": "OTHER-DEMO-ACCOUNT"},
        {"expected_demo_account_id": None},
        {"authenticated_account_id": None},
        {"lease_valid": False},
        {"current_fencing_token": 8},
        {"kill_switch_state": KillSwitchState.BLOCKING},
    ],
)
def test_each_authority_veto_blocks_create_before_fake_transport(
    overrides: dict[str, object],
) -> None:
    execution, transport, _store = core()

    with pytest.raises(DemoExecutionError):
        execution.submit(request(), metadata(), authority(**overrides), now=NOW)

    assert transport.broker_create_call_count == 0


def test_unsupported_instrument_and_global_position_limit_block_create() -> None:
    execution, transport, _store = core()
    unsupported = request(epic="CS.D.UNRELATED.MINI.IP")
    with pytest.raises(DemoExecutionError, match="approved Demo registry"):
        execution.submit(unsupported, metadata(epic=unsupported.epic), authority(), now=NOW)
    with pytest.raises(DemoExecutionError, match="global position limit"):
        execution.submit(request(), metadata(), authority(global_position_count=1), now=NOW)
    assert transport.broker_create_call_count == 0


@pytest.mark.parametrize(
    "changed_metadata",
    [
        {"instrument_currency": None},
        {"pip_scale": None},
        {"minimum_deal_size": None},
        {"minimum_stop_distance": None},
        {"market_status": "CLOSED"},
        {"observed_at": NOW - timedelta(seconds=61)},
    ],
)
def test_missing_or_invalid_market_metadata_blocks_create(
    changed_metadata: dict[str, object],
) -> None:
    execution, transport, _store = core()

    with pytest.raises(DemoExecutionError):
        execution.submit(request(), metadata(**changed_metadata), authority(), now=NOW)

    assert transport.broker_create_call_count == 0


def test_size_and_stop_below_broker_minimum_block_create() -> None:
    execution, transport, _store = core()
    with pytest.raises(DemoExecutionError, match="size"):
        execution.submit(request(size=Decimal("0.99")), metadata(), authority(), now=NOW)
    with pytest.raises(DemoExecutionError, match="stop distance"):
        execution.submit(request(stop_distance=Decimal("0.9")), metadata(), authority(), now=NOW)
    assert transport.broker_create_call_count == 0


def test_deal_reference_is_deterministic_safe_and_non_secret() -> None:
    intent = UUID("00000000-0000-0000-0000-000000000001")
    cycle = UUID("00000000-0000-0000-0000-000000000002")
    first = deterministic_deal_reference(
        intent_id=intent,
        global_cycle_id=cycle,
        epic=EPIC,
        configuration_identity=CONFIGURATION_ID,
    )
    assert first == deterministic_deal_reference(
        intent_id=intent,
        global_cycle_id=cycle,
        epic=EPIC,
        configuration_identity=CONFIGURATION_ID,
    )
    assert first != deterministic_deal_reference(
        intent_id=uuid4(),
        global_cycle_id=cycle,
        epic=EPIC,
        configuration_identity=CONFIGURATION_ID,
    )
    assert len(first) <= 30
    assert all(character.isalnum() or character in "_-" for character in first)
    assert "ACCOUNT" not in first


@pytest.mark.parametrize("direction", [DemoDirection.BUY, DemoDirection.SELL])
def test_market_payload_preserves_exact_economics_without_hardcoded_currency(
    direction: DemoDirection,
) -> None:
    execution, transport, _store = core()
    item = request(direction=direction, size=Decimal("1.23"), currency="GBP")

    record = execution.submit(item, metadata(), authority(), now=NOW)

    assert record.lifecycle is DemoExecutionLifecycle.SUBMITTED
    assert transport.create_payloads == [
        {
            "currencyCode": "GBP",
            "dealReference": item.deal_reference,
            "direction": direction.value,
            "epic": EPIC,
            "expiry": "DFB",
            "forceOpen": True,
            "guaranteedStop": False,
            "limitDistance": "3.0",
            "orderType": "MARKET",
            "size": "1.23",
            "stopDistance": "2.0",
        }
    ]
    assert "level" not in {key.casefold() for key in transport.create_payloads[0]}


def test_request_requires_exactly_one_stop_and_limit_representation() -> None:
    with pytest.raises(DemoExecutionError, match="exactly one stop"):
        request(stop_distance=None, stop_level=None)
    with pytest.raises(DemoExecutionError, match="exactly one limit"):
        request(limit_distance=Decimal("3"), limit_level=Decimal("0.851"))


def test_duplicate_intent_submits_at_most_once() -> None:
    execution, transport, _store = core()
    item = request()

    first = execution.submit(item, metadata(), authority(), now=NOW)
    second = execution.submit(item, metadata(), authority(), now=NOW)

    assert first == second
    assert transport.broker_create_call_count == 1


def test_timeout_and_restart_require_reconciliation_without_second_post() -> None:
    execution, transport, store = core()
    item = request()
    transport.queue_create(TimeoutError("lost response"))

    uncertain = execution.submit(item, metadata(), authority(), now=NOW)
    retry = execution.submit(item, metadata(), authority(), now=NOW)
    restarted = DemoExecutionCore(transport=transport, store=store)
    reconciled = restarted.reconcile_open(item.intent_id)

    assert uncertain.lifecycle is DemoExecutionLifecycle.AMBIGUOUS
    assert retry.lifecycle is DemoExecutionLifecycle.AMBIGUOUS
    assert reconciled.lifecycle is DemoExecutionLifecycle.AMBIGUOUS
    assert transport.broker_create_call_count == 1
    assert transport.confirmation_read_count == 1


def test_restart_after_submitted_reconciles_before_any_second_post() -> None:
    execution, transport, store = core()
    item = request()
    execution.submit(item, metadata(), authority(), now=NOW)

    restarted = DemoExecutionCore(transport=transport, store=store)
    record = restarted.reconcile_open(item.intent_id)

    assert record.lifecycle is DemoExecutionLifecycle.AMBIGUOUS
    assert transport.broker_create_call_count == 1
    assert transport.confirmation_read_count == 1


def test_second_intent_in_a_global_execution_cycle_cannot_submit() -> None:
    execution, transport, _store = core()
    cycle = uuid4()
    first = request(cycle_id=cycle)
    second = request(cycle_id=cycle, epic="CS.D.EURUSD.CEFM.IP")

    execution.submit(first, metadata(), authority(), now=NOW)
    with pytest.raises(DemoExecutionError, match="global execution cycle"):
        execution.submit(second, metadata(epic=second.epic), authority(), now=NOW)

    assert transport.broker_create_call_count == 1


def test_accepted_confirmation_and_matching_position_open_lifecycle() -> None:
    execution, transport, _store = core()
    item = request()
    execution.submit(item, metadata(), authority(), now=NOW)
    position = DemoPosition("D-1", item.epic, item.direction, item.size)
    transport.queue_confirmation(item.deal_reference, confirmation(item))
    transport.queue_positions((position,))

    record = execution.reconcile_open(item.intent_id)

    assert record.lifecycle is DemoExecutionLifecycle.OPEN_RECONCILED
    assert record.position == position


def test_rejected_confirmation_and_mismatched_confirmation_facts_fail_closed() -> None:
    for overrides in (
        {"status": DemoConfirmationStatus.REJECTED},
        {"epic": "CS.D.UNRELATED.MINI.IP"},
        {"direction": DemoDirection.SELL},
        {"size": Decimal("2.00")},
        {"deal_reference": "DQ01_wrong"},
    ):
        execution, transport, _store = core()
        item = request()
        execution.submit(item, metadata(), authority(), now=NOW)
        transport.queue_confirmation(item.deal_reference, confirmation(item, **overrides))
        record = execution.reconcile_open(item.intent_id)
        expected = (
            DemoExecutionLifecycle.CONFIRMED_REJECTED
            if overrides.get("status") is DemoConfirmationStatus.REJECTED
            else DemoExecutionLifecycle.FAILED_SAFE
        )
        assert record.lifecycle is expected


@pytest.mark.parametrize(
    "positions", [(), (DemoPosition("D-1", EPIC, DemoDirection.BUY, Decimal("1.23")),) * 2]
)
def test_accepted_confirmation_without_exactly_one_position_fails_safe(
    positions: tuple[DemoPosition, ...],
) -> None:
    execution, transport, _store = core()
    item = request()
    execution.submit(item, metadata(), authority(), now=NOW)
    transport.queue_confirmation(item.deal_reference, confirmation(item))
    transport.queue_positions(positions)

    record = execution.reconcile_open(item.intent_id)

    assert record.lifecycle is DemoExecutionLifecycle.FAILED_SAFE
    assert transport.broker_create_call_count == 1


def test_close_uses_exact_reconciled_deal_and_requires_confirmation_and_absence() -> None:
    execution, transport, _store = core()
    item = request()
    opened, position = open_record(execution, transport, item)
    close_authority = authority(global_position_count=1)

    requested = execution.request_close(item.intent_id, close_authority)
    duplicate = execution.request_close(item.intent_id, close_authority)
    assert requested == duplicate
    assert requested.close_submission is not None
    assert transport.close_payloads == [
        {
            "dealId": position.deal_id,
            "dealReference": requested.close_submission.deal_reference,
            "direction": "SELL",
            "orderType": "MARKET",
            "size": "1.23",
        }
    ]
    transport.queue_confirmation(
        requested.close_submission.deal_reference,
        DemoConfirmation(
            requested.close_submission.deal_reference,
            position.deal_id,
            DemoConfirmationStatus.ACCEPTED,
            "CLOSED",
            position.epic,
            DemoDirection.SELL,
            position.size,
        ),
    )
    transport.queue_positions(())

    closed = execution.reconcile_close(item.intent_id)

    assert opened.lifecycle is DemoExecutionLifecycle.OPEN_RECONCILED
    assert closed.lifecycle is DemoExecutionLifecycle.CLOSED_RECONCILED
    assert transport.broker_close_call_count == 1


def test_close_kill_switch_and_missing_confirmation_fail_closed_without_retry() -> None:
    execution, transport, _store = core()
    item = request()
    opened, _position = open_record(execution, transport, item)
    with pytest.raises(DemoExecutionError, match="kill switch"):
        execution.request_close(
            item.intent_id,
            authority(global_position_count=1, kill_switch_state=KillSwitchState.BLOCKING),
        )
    assert transport.broker_close_call_count == 0

    requested = execution.request_close(item.intent_id, authority(global_position_count=1))
    reconciled = execution.reconcile_close(item.intent_id)
    assert opened.lifecycle is DemoExecutionLifecycle.OPEN_RECONCILED
    assert requested.lifecycle is DemoExecutionLifecycle.CLOSE_REQUESTED
    assert reconciled.lifecycle is DemoExecutionLifecycle.AMBIGUOUS
    assert transport.broker_close_call_count == 1


def test_evidence_counters_are_sanitized_and_real_network_is_absent() -> None:
    execution, transport, _store = core()
    evidence = execution.evidence(authority())
    tree = ast.parse(Path("src/ig_trader/demo_execution.py").read_text(encoding="utf-8"))
    imported_modules = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        for alias in (node.names if isinstance(node, ast.Import) else ())
    }
    from_modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    imported_modules.update(module.split(".")[0] for module in from_modules)

    assert evidence == {
        "broker_create_call_count": 0,
        "broker_close_call_count": 0,
        "broker_update_call_count": 0,
        "confirmation_read_count": 0,
        "position_read_count": 0,
        "demo_order_authority": True,
        "execution_mode": "DEMO_EXECUTION",
        "kill_switch_state": "RELEASED",
    }
    assert not imported_modules.intersection({"httpx", "requests", "socket"})
    assert not from_modules.intersection(
        {
            "src.ig_trader.config",
            "src.ig_trader.http_client",
            "src.ig_trader.session",
        }
    )
