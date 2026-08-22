"""G2 broker-isolated lifecycle, recovery, idempotency, and network proofs."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.ig_trader.offline_paper.conductor import (
    FaultPoint,
    FrozenV1Config,
    InjectedCrash,
    OfflinePaperConductor,
    PortfolioRisk,
    raising_fault,
)
from src.ig_trader.offline_paper.domain import (
    AccountSnapshot,
    BrokerOrder,
    LifecycleState,
    Position,
    RunStatus,
    Side,
)
from src.ig_trader.offline_paper.fixture import LocalFixtureData
from src.ig_trader.offline_paper.paper_broker import PaperBroker
from src.ig_trader.offline_paper.persistence import TradeIntentStore

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "fixtures" / "g2-offline-paper-market.json"
EXPECTED_STATES = (
    LifecycleState.SIGNAL_DETECTED,
    LifecycleState.INTENT_CREATED,
    LifecycleState.ORDER_SUBMITTED,
    LifecycleState.ORDER_ACCEPTED,
    LifecycleState.POSITION_OPEN,
    LifecycleState.EXIT_REQUESTED,
    LifecycleState.POSITION_CLOSED,
    LifecycleState.RECONCILED,
)


def _components(
    tmp_path: Path,
    *,
    fault: FaultPoint | None = None,
    rejected_epics: frozenset[str] = frozenset(),
):
    source = LocalFixtureData(INPUT)
    intents = TradeIntentStore(tmp_path / "trade-intents.db")
    broker = PaperBroker(
        tmp_path / "paper-broker.db",
        account_id=source.account.account_id,
        currency=source.account.currency,
        starting_balance=source.account.starting_balance,
        rejected_epics=rejected_epics,
    )
    conductor = OfflinePaperConductor(
        market_data=source,
        historical_data=source,
        source=source,
        execution=broker,
        account=broker,
        reconciliation=broker,
        intents=intents,
        fault_hook=raising_fault(fault) if fault else None,
    )
    return source, intents, broker, conductor


def test_full_lifecycle_persists_complete_trade_intent_and_lineage(tmp_path: Path) -> None:
    source, intents, broker, conductor = _components(tmp_path)

    result = conductor.run()

    assert result.status is RunStatus.COMPLETE
    assert result.lifecycle_states == EXPECTED_STATES
    stored = intents.intents()
    assert stored is not None and len(stored) == 1
    intent = stored[0]
    assert intent.lifecycle_state is LifecycleState.RECONCILED
    assert intent.execution_mode.value == "OFFLINE_PAPER"
    assert intent.epic == "CS.D.EURGBP.MINI.IP"
    assert intent.strategy == "Scalper"
    assert intent.strategy_version == "Scalper:rsi-adx-v1"
    assert intent.confidence >= 0.70
    assert intent.spread_pips <= 1.2
    assert intent.risk_decision.allowed
    assert intent.risk_decision.code == "ALLOWED"
    assert intent.size > 0
    assert intent.stop_level < intent.signal_inputs["requested_entry"] < intent.target_level
    assert intent.source_fingerprint
    assert intent.source_candle_references[0]["candle_count"] == 60
    assert intent.source_candle_references[0]["fixture_sha256"] == source.document_fingerprint
    lineage = intents.lineage(source.cycle_id)
    assert lineage is not None
    assert {
        "MARKET_CANDLE_INPUT",
        "STRATEGY_CALCULATION",
        "SIGNAL",
        "CANDIDATE",
        "RANKING",
        "PORTFOLIO_RISK",
        "POSITION_SIZING",
        "TRADE_INTENT",
        "EXECUTION",
        "CONFIRMATION",
        "POSITION",
        "EXIT",
        "RECONCILIATION",
    }.issubset({item["phase"] for item in lineage})
    reconciliation = broker.reconciliation_snapshot(as_of=datetime.now(UTC))
    assert reconciliation is not None
    assert reconciliation.account.positions == ()
    assert len(reconciliation.orders) == 1
    assert len(reconciliation.fills) == 1
    assert len(reconciliation.exits) == 1


@pytest.mark.parametrize(
    "fault",
    [
        FaultPoint.BEFORE_TRADE_INTENT,
        FaultPoint.AFTER_INTENT_CREATED,
        FaultPoint.AFTER_ORDER_SUBMITTED,
        FaultPoint.AFTER_PAPER_SUBMISSION,
        FaultPoint.AFTER_ORDER_ACCEPTED,
        FaultPoint.AFTER_POSITION_OPEN,
        FaultPoint.AFTER_EXIT_REQUESTED,
        FaultPoint.AFTER_PAPER_CLOSE,
        FaultPoint.AFTER_POSITION_CLOSED,
    ],
)
def test_restart_recovers_each_durable_boundary_without_duplicate_order(
    tmp_path: Path,
    fault: FaultPoint,
) -> None:
    _, _, _, crashing = _components(tmp_path, fault=fault)
    with pytest.raises(InjectedCrash, match=fault.value):
        crashing.run()

    _, intents, broker, restarted = _components(tmp_path)
    result = restarted.run()

    assert result.status is RunStatus.COMPLETE
    assert result.lifecycle_states == EXPECTED_STATES
    stored = intents.intents()
    assert stored is not None and len(stored) == 1
    snapshot = broker.reconciliation_snapshot(as_of=datetime.now(UTC))
    assert snapshot is not None
    assert len(snapshot.orders) == len(snapshot.fills) == len(snapshot.exits) == 1
    assert snapshot.account.positions == ()


def test_duplicate_cycle_processing_is_idempotent(tmp_path: Path) -> None:
    _, intents, broker, conductor = _components(tmp_path)
    first = conductor.run()
    second = conductor.run()

    assert first.status is RunStatus.COMPLETE
    assert second.status is RunStatus.COMPLETE
    assert second.idempotent_restart
    assert len(intents.intents() or ()) == 1
    snapshot = broker.reconciliation_snapshot(as_of=datetime.now(UTC))
    assert snapshot is not None
    assert len(snapshot.orders) == len(snapshot.fills) == len(snapshot.exits) == 1


def test_corrupt_intent_state_blocks_without_new_paper_order(tmp_path: Path) -> None:
    _, _, _, crashing = _components(tmp_path, fault=FaultPoint.AFTER_INTENT_CREATED)
    with pytest.raises(InjectedCrash):
        crashing.run()
    with sqlite3.connect(tmp_path / "trade-intents.db") as connection:
        connection.execute("UPDATE trade_intents SET payload='not-json'")

    _, intents, broker, restarted = _components(tmp_path)
    result = restarted.run()

    assert result.status is RunStatus.BLOCKED
    assert result.reason == "PERSISTED_STATE_UNKNOWN"
    assert intents.intents() is None
    snapshot = broker.reconciliation_snapshot(as_of=datetime.now(UTC))
    assert snapshot is not None and snapshot.orders == ()


def test_orphan_accepted_intent_fails_safe_without_resubmission(tmp_path: Path) -> None:
    source, intents, _, crashing = _components(tmp_path, fault=FaultPoint.AFTER_INTENT_CREATED)
    with pytest.raises(InjectedCrash):
        crashing.run()
    intent = (intents.intents() or ())[0]
    assert intents.transition(
        intent.intent_id,
        LifecycleState.ORDER_SUBMITTED,
        reason="TEST_ORPHAN_SUBMITTED",
        occurred_at=source.evaluation_time,
    )
    assert intents.transition(
        intent.intent_id,
        LifecycleState.ORDER_ACCEPTED,
        reason="TEST_ORPHAN_ACCEPTED",
        occurred_at=source.evaluation_time,
    )

    _, checked, broker, restarted = _components(tmp_path)
    result = restarted.run()

    assert result.status is RunStatus.BLOCKED
    assert result.reason == "ACCEPTED_POSITION_STATE_UNKNOWN"
    assert checked.get(intent.intent_id).lifecycle_state is LifecycleState.FAILED_SAFE
    snapshot = broker.reconciliation_snapshot(as_of=datetime.now(UTC))
    assert snapshot is not None and snapshot.orders == ()


def test_mismatched_intent_blocks_new_execution(tmp_path: Path) -> None:
    _, _, _, crashing = _components(tmp_path, fault=FaultPoint.AFTER_INTENT_CREATED)
    with pytest.raises(InjectedCrash):
        crashing.run()
    with sqlite3.connect(tmp_path / "trade-intents.db") as connection:
        row = connection.execute("SELECT payload FROM trade_intents").fetchone()
        payload = json.loads(row[0])
        payload["epic"] = "MISMATCHED.EPIC"
        connection.execute(
            "UPDATE trade_intents SET epic=?,payload=?",
            ("MISMATCHED.EPIC", json.dumps(payload, sort_keys=True, separators=(",", ":"))),
        )

    _, _, broker, restarted = _components(tmp_path)
    result = restarted.run()

    assert result.status is RunStatus.BLOCKED
    assert result.reason == "MISMATCHED_INTENT"
    snapshot = broker.reconciliation_snapshot(as_of=datetime.now(UTC))
    assert snapshot is not None and snapshot.orders == ()


def test_orphan_paper_order_blocks_before_strategy_or_new_intent(tmp_path: Path) -> None:
    source, intents, broker, conductor = _components(tmp_path)
    quote = source.quote("CS.D.EURGBP.MINI.IP", as_of=source.evaluation_time)
    assert quote is not None
    order = BrokerOrder(
        "PAPER-ORDER-ORPHAN",
        "orphan-intent",
        quote.epic,
        Side.BUY,
        0.1,
        quote.offer,
        quote.offer - 0.0004,
        quote.offer + 0.0006,
        quote.pip_size,
        quote.pip_value_account_currency,
        source.evaluation_time,
    )
    assert broker.submit(order).accepted

    result = conductor.run()

    assert result.status is RunStatus.BLOCKED
    assert result.reason == "ORPHAN_PAPER_BROKER_STATE"
    assert intents.intents() == ()


def test_portfolio_risk_is_absolute_veto_for_unknown_or_existing_position(
    tmp_path: Path,
) -> None:
    source, _, _, conductor = _components(tmp_path)
    candidate = conductor._evaluate_cycle()[0]
    policy = PortfolioRisk(FrozenV1Config())
    unknown = AccountSnapshot(
        "A",
        "EUR",
        10_000.0,
        10_000.0,
        (),
        source.evaluation_time,
        False,
    )
    assert not policy.evaluate(
        candidate,
        account=unknown,
        executions_in_cycle=0,
        stop_pips=4.0,
    ).allowed
    position = Position(
        "P",
        "I",
        "CS.D.GBPUSD.MINI.IP",
        Side.BUY,
        0.1,
        1.28,
        1.279,
        1.2815,
        0.0001,
        1.0,
        source.evaluation_time,
    )
    occupied = AccountSnapshot(
        "A",
        "EUR",
        10_000.0,
        10_000.0,
        (position,),
        source.evaluation_time,
        True,
    )
    decision = policy.evaluate(
        candidate,
        account=occupied,
        executions_in_cycle=0,
        stop_pips=4.0,
    )
    assert not decision.allowed
    assert decision.code == "TOTAL_POSITION_LIMIT"


def test_risk_rejection_is_idempotent_across_restart_and_creates_no_order(
    tmp_path: Path,
) -> None:
    _, intents, broker, conductor = _components(tmp_path)
    with sqlite3.connect(tmp_path / "paper-broker.db") as connection:
        connection.execute(
            "UPDATE paper_accounts SET balance=? WHERE account_id=?",
            (9_400.0, "G2-OFFLINE-PAPER"),
        )

    first = conductor.run()
    second = conductor.run()

    assert first.status is RunStatus.NO_TRADE
    assert second.status is RunStatus.NO_TRADE
    assert first.reason == second.reason == "DAILY_LOSS_LIMIT"
    assert first.lifecycle_states == (LifecycleState.RISK_REJECTED,)
    assert intents.intents() == ()
    snapshot = broker.reconciliation_snapshot(as_of=datetime.now(UTC))
    assert snapshot is not None
    assert snapshot.orders == snapshot.fills == snapshot.exits == ()
    assert snapshot.account.positions == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rsi_period", 8),
        ("confidence_threshold", 0.71),
        ("adx_threshold", 21.0),
        ("warmup_candles", 61),
        ("stop_atr_multiplier", 2.1),
        ("reward_to_risk", 1.6),
        ("maximum_stop_pips", 13.0),
        ("maximum_spread_pips", 1.3),
        ("maximum_spread_to_target_ratio", 0.16),
        ("maximum_total_positions", 2),
        ("maximum_positions_per_instrument", 2),
        ("maximum_executions_per_cycle", 2),
    ],
)
def test_frozen_v1_rejects_parameter_changes(field: str, value: object) -> None:
    with pytest.raises(ValueError, match="frozen V1"):
        FrozenV1Config(**{field: value})


def test_paper_broker_deterministic_rejection_has_no_position(tmp_path: Path) -> None:
    rejected = frozenset({"CS.D.EURGBP.MINI.IP"})
    _, intents, broker, conductor = _components(tmp_path, rejected_epics=rejected)

    result = conductor.run()

    assert result.status is RunStatus.COMPLETE
    assert LifecycleState.ORDER_REJECTED in result.lifecycle_states
    assert LifecycleState.POSITION_OPEN not in result.lifecycle_states
    assert (intents.intents() or ())[0].lifecycle_state is LifecycleState.RECONCILED
    snapshot = broker.reconciliation_snapshot(as_of=datetime.now(UTC))
    assert snapshot is not None and snapshot.account.positions == ()


def test_only_exact_paper_broker_may_satisfy_execution_port(tmp_path: Path) -> None:
    source = LocalFixtureData(INPUT)
    intents = TradeIntentStore(tmp_path / "intents.db")

    class FakeExecution:
        pass

    fake = FakeExecution()
    with pytest.raises(TypeError, match="PaperBroker"):
        OfflinePaperConductor(
            market_data=source,
            historical_data=source,
            source=source,
            execution=fake,
            account=fake,
            reconciliation=fake,
            intents=intents,
        )


def test_launcher_proves_zero_network_and_broker_calls(tmp_path: Path) -> None:
    state = tmp_path / "state"
    evidence_json = tmp_path / "g2.json"
    evidence_md = tmp_path / "g2.md"
    command = [
        sys.executable,
        "-m",
        "src.ig_trader.offline_paper",
        "--mode",
        "OFFLINE_PAPER",
        "--input",
        str(INPUT),
        "--state-directory",
        str(state),
        "--evidence-json",
        str(evidence_json),
        "--evidence-markdown",
        str(evidence_md),
    ]
    environment = {**os.environ, "PYTHONPATH": str(ROOT)}
    repository_database = ROOT / "trading.db"
    database_before = (
        repository_database.read_bytes()
        if repository_database.exists()
        else None
    )

    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(evidence_json.read_text(encoding="utf-8"))
    assert evidence["final_classification"] == "PASS"
    network = evidence["network_isolation"]
    assert network["network_call_count"] == 0
    assert network["ig_rest_call_count"] == 0
    assert network["lightstreamer_connection_count"] == 0
    assert network["order_endpoint_call_count"] == 0
    assert network["credential_resolution_count"] == 0
    assert not network["ig_rest_instantiated"]
    assert not network["lightstreamer_instantiated"]
    assert not network["credentials_resolved"]
    database_after = (
        repository_database.read_bytes()
        if repository_database.exists()
        else None
    )
    assert database_after == database_before


def test_isolation_blocks_socket_clients_credentials_and_order_modules() -> None:
    code = """
from src.ig_trader.offline_paper.isolation import OfflineIsolationError, activate
metrics = activate('OFFLINE_PAPER')
import socket
for action in (
    lambda: socket.socket(),
    lambda: __import__('src.ig_trader.session', fromlist=['SessionManager']),
    lambda: __import__('src.ig_trader.streaming', fromlist=['StreamManager']),
    lambda: __import__('src.ig_trader.config', fromlist=['settings']),
    lambda: __import__('src.ig_trader.execution', fromlist=['ExecutionEngine']),
):
    try:
        action()
    except OfflineIsolationError:
        pass
    else:
        raise AssertionError('prohibited capability was not blocked')
assert metrics.network_call_count == 0
assert metrics.ig_rest_call_count == 0
assert metrics.lightstreamer_connection_count == 0
assert metrics.order_endpoint_call_count == 0
assert metrics.credential_resolution_count == 0
assert metrics.blocked_network_attempt_count == 1
assert metrics.blocked_ig_import_attempt_count == 1
assert metrics.blocked_lightstreamer_import_attempt_count == 1
assert metrics.blocked_credential_import_attempt_count == 1
assert metrics.blocked_order_import_attempt_count == 1
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_live_mode_is_rejected_before_state_or_credentials(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.ig_trader.offline_paper",
            "--mode",
            "LIVE",
            "--input",
            str(INPUT),
            "--state-directory",
            str(tmp_path / "state"),
            "--evidence-json",
            str(tmp_path / "evidence.json"),
            "--evidence-markdown",
            str(tmp_path / "evidence.md"),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "evidence.json").exists()


def test_package_and_offline_launcher_import_without_config_or_database_side_effect(
    tmp_path: Path,
) -> None:
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(ROOT)!r});"
        "import src.ig_trader;"
        "import src.ig_trader.offline_paper.__main__;"
        "assert 'src.ig_trader.config' not in sys.modules;"
        "assert 'src.ig_trader.session' not in sys.modules;"
        "assert 'src.ig_trader.streaming' not in sys.modules;"
        "assert 'src.ig_trader.database' not in sys.modules"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert not (tmp_path / "trading.db").exists()
