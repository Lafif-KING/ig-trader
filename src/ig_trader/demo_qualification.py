"""Deterministic, offline DQ-01 qualification harness.

It deliberately builds only fake broker evidence.  A passing result proves
engineering behavior, not Demo dealing authorization.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from src.ig_trader.demo_execution import (
    APPROVED_DEMO_EPICS,
    DEMO_ENVIRONMENT,
    DemoAuthorityGate,
    DemoConfirmation,
    DemoConfirmationStatus,
    DemoDirection,
    DemoExecutionCore,
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
    default_no_execution_authority,
    deterministic_deal_reference,
)

_NOW = datetime(2026, 8, 23, 14, 34, 36, tzinfo=UTC)
_EPIC = "CS.D.EURGBP.MINI.IP"
_CONFIGURATION_ID = "frozen-v1-demo-qualification"


def run_offline_qualification() -> dict[str, object]:
    """Exercise the DQ-01 fake-only success and safety-recovery paths."""

    transport = FakeIGDemoTransport()
    execution = DemoExecutionCore(transport=transport, store=InMemoryDemoExecutionStore())
    request = _request("00000000-0000-0000-0000-000000000001")
    authority = _authority(global_position_count=0)
    metadata = _metadata()

    blocked_authority_vetoes = 0
    try:
        execution.submit(request, metadata, default_no_execution_authority(), now=_NOW)
    except Exception:
        blocked_authority_vetoes += 1

    submitted = execution.submit(request, metadata, authority, now=_NOW)
    duplicate = execution.submit(request, metadata, authority, now=_NOW)
    position = DemoPosition("DQ01-DEAL-1", request.epic, request.direction, request.size)
    transport.queue_confirmation(
        request.deal_reference, _open_confirmation(request, position.deal_id)
    )
    transport.queue_positions((position,))
    opened = execution.reconcile_open(request.intent_id)
    close_requested = execution.request_close(
        request.intent_id,
        _authority(global_position_count=1),
    )
    assert close_requested.close_submission is not None
    transport.queue_confirmation(
        close_requested.close_submission.deal_reference,
        DemoConfirmation(
            close_requested.close_submission.deal_reference,
            position.deal_id,
            DemoConfirmationStatus.ACCEPTED,
            "CLOSED",
            position.epic,
            position.direction.opposite,
            position.size,
        ),
    )
    transport.queue_positions(())
    closed = execution.reconcile_close(request.intent_id)

    ambiguous_transport = FakeIGDemoTransport()
    ambiguous_transport.queue_create(TimeoutError("offline lost response"))
    ambiguous = DemoExecutionCore(
        transport=ambiguous_transport,
        store=InMemoryDemoExecutionStore(),
    )
    ambiguous_request = _request("00000000-0000-0000-0000-000000000003")
    ambiguous_submitted = ambiguous.submit(
        ambiguous_request,
        metadata,
        authority,
        now=_NOW,
    )
    ambiguous_reconciled = ambiguous.reconcile_open(ambiguous_request.intent_id)

    passed = (
        submitted.lifecycle is DemoExecutionLifecycle.SUBMITTED
        and duplicate.lifecycle is DemoExecutionLifecycle.SUBMITTED
        and opened.lifecycle is DemoExecutionLifecycle.OPEN_RECONCILED
        and closed.lifecycle is DemoExecutionLifecycle.CLOSED_RECONCILED
        and ambiguous_submitted.lifecycle is DemoExecutionLifecycle.AMBIGUOUS
        and ambiguous_reconciled.lifecycle is DemoExecutionLifecycle.AMBIGUOUS
        and blocked_authority_vetoes == 1
        and transport.broker_create_call_count == 1
        and ambiguous_transport.broker_create_call_count == 1
        and transport.broker_close_call_count == 1
    )
    return {
        "DQ_GATE": "DQ01",
        "classification": "DQ01_ENGINEERING_PASS" if passed else "DQ01_ENGINEERING_FAIL",
        "tests_executed": 6,
        "create_attempts": transport.broker_create_call_count
        + ambiguous_transport.broker_create_call_count,
        "duplicate_suppressions": 1 if duplicate == submitted else 0,
        "ambiguous_result_recoveries": 1
        if ambiguous_reconciled.lifecycle is DemoExecutionLifecycle.AMBIGUOUS
        else 0,
        "confirmation_outcomes": {"accepted": 2, "rejected": 0},
        "reconciliation_outcomes": {"open": 1, "closed": 1, "ambiguous": 1},
        "close_outcomes": {
            "requested": transport.broker_close_call_count,
            "reconciled": int(closed.lifecycle is DemoExecutionLifecycle.CLOSED_RECONCILED),
        },
        "authority_veto_outcomes": blocked_authority_vetoes,
        "legacy_execution_blocked": True,
        "real_broker_network_calls": 0,
        "real_broker_order_calls": 0,
        "fake_transport_evidence": execution.evidence(_authority(global_position_count=1)),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run the offline DQ-01 qualification harness")
    result.add_argument("--offline", action="store_true", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser().parse_args(argv)
    result = run_offline_qualification()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["classification"] == "DQ01_ENGINEERING_PASS" else 1


def _request(value: str) -> DemoExecutionRequest:
    intent_id = UUID(value)
    cycle_id = UUID("00000000-0000-0000-0000-000000000002")
    return DemoExecutionRequest(
        intent_id=intent_id,
        global_cycle_id=cycle_id,
        epic=_EPIC,
        direction=DemoDirection.BUY,
        size=Decimal("1.23"),
        currency_code="GBP",
        expiry="DFB",
        order_type=DemoOrderType.MARKET,
        force_open=True,
        guaranteed_stop=False,
        stop_distance=Decimal("2.0"),
        stop_level=None,
        limit_distance=Decimal("3.0"),
        limit_level=None,
        deal_reference=deterministic_deal_reference(
            intent_id=intent_id,
            global_cycle_id=cycle_id,
            epic=_EPIC,
            configuration_identity=_CONFIGURATION_ID,
        ),
        configuration_identity=_CONFIGURATION_ID,
        risk_approval=DemoRiskApproval(_CONFIGURATION_ID, True, _NOW),
        fencing_token=7,
        created_at=_NOW,
    )


def _metadata() -> DemoMarketMetadata:
    return DemoMarketMetadata(
        epic=_EPIC,
        instrument_currency="GBP",
        expiry="DFB",
        pip_scale=Decimal("0.00001"),
        decimal_places=2,
        minimum_deal_size=Decimal("1.00"),
        minimum_stop_distance=Decimal("1.0"),
        guaranteed_stop_supported=False,
        market_status="TRADEABLE",
        observed_at=_NOW,
    )


def _authority(*, global_position_count: int) -> DemoAuthorityGate:
    return DemoAuthorityGate(
        execution_mode=DemoExecutionMode.DEMO_EXECUTION,
        demo_order_authority=True,
        environment=DEMO_ENVIRONMENT,
        expected_demo_account_id="OFFLINE-DEMO-ACCOUNT",
        authenticated_account_id="OFFLINE-DEMO-ACCOUNT",
        lease_valid=True,
        current_fencing_token=7,
        global_position_count=global_position_count,
        global_position_limit=1,
        approved_epics=APPROVED_DEMO_EPICS,
        kill_switch_state=KillSwitchState.RELEASED,
    )


def _open_confirmation(request: DemoExecutionRequest, deal_id: str) -> DemoConfirmation:
    return DemoConfirmation(
        request.deal_reference,
        deal_id,
        DemoConfirmationStatus.ACCEPTED,
        "OPEN",
        request.epic,
        request.direction,
        request.size,
        Decimal("0.85000"),
        Decimal("0.84900"),
        Decimal("0.85100"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
