"""G3B package, alignment, gap, execution, determinism, and isolation proofs."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.ig_trader.g3a_data import RESOLUTION_MINUTES, CanonicalCandle
from src.ig_trader.g3b_replay.account_state import (
    ACCEPTED_G2_COMMIT_SHA,
    ACCEPTED_G2_FIXTURE_SHA256,
    G2QualificationState,
    QualificationAccountStateGap,
)
from src.ig_trader.g3b_replay.data import (
    AUTHORITATIVE_GAP_EPIC,
    AUTHORITATIVE_GAP_TIMESTAMP,
    EXPECTED_DATASET_FINGERPRINT,
    EXPECTED_INVENTORY,
    EXPECTED_MANIFEST_SHA256,
    EXPECTED_PACKAGE_FINGERPRINT,
    ArtifactIntegrityError,
    ReplayDataset,
    _verify_package,
    verify_and_load_package,
)
from src.ig_trader.g3b_replay.engine import (
    ExactReplayEngine,
    build_trade_intent,
    evaluate_candidate,
    frozen_replay_configuration_hash,
    resolve_exit,
)
from src.ig_trader.g3b_replay.reporting import canonical_bytes
from src.ig_trader.offline_paper.conductor import FrozenV1Config, PortfolioRisk
from src.ig_trader.offline_paper.domain import (
    AccountSnapshot,
    Position,
    Quote,
    RiskDecision,
    Side,
    Signal,
    TradeCandidate,
)
from src.ig_trader.offline_paper.paper_broker import PaperBroker

ROOT = Path(__file__).resolve().parents[1]
POINTER = ROOT / "artifacts" / "g3a" / "external-package.json"
QUALIFICATION_FIXTURE = ROOT / "fixtures" / "g2-offline-paper-market.json"


def _package_root() -> Path:
    document = json.loads(POINTER.read_text(encoding="utf-8"))
    return Path(document["local_package_path"])


@pytest.fixture(scope="module")
def accepted_dataset() -> ReplayDataset:
    package_root = _package_root()
    if not package_root.is_dir():
        pytest.skip("external accepted G3A package is not mounted")
    return verify_and_load_package(package_root)


@pytest.fixture(scope="module")
def qualification_state() -> G2QualificationState:
    return G2QualificationState.load(QUALIFICATION_FIXTURE)


@pytest.fixture(scope="module")
def replay_document(
    accepted_dataset: ReplayDataset,
    qualification_state: G2QualificationState,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, object]:
    broker = qualification_state.create_paper_broker(
        tmp_path_factory.mktemp("g3b-replay") / "paper-broker.db"
    )
    return ExactReplayEngine(
        accepted_dataset,
        commit_sha="0" * 40,
        network_metrics=_zero_network(),
        qualification_state=qualification_state,
        paper_broker=broker,
    ).run()


def test_exact_package_manifest_normalized_hashes_dataset_and_inventory(
    accepted_dataset: ReplayDataset,
) -> None:
    verification = accepted_dataset.verification

    assert verification.package_fingerprint == EXPECTED_PACKAGE_FINGERPRINT
    assert verification.manifest_sha256 == EXPECTED_MANIFEST_SHA256
    assert verification.dataset_fingerprint == EXPECTED_DATASET_FINGERPRINT
    assert verification.file_count == verification.read_only_file_count == 125
    assert set(accepted_dataset.candles) == EXPECTED_INVENTORY
    assert len(verification.series) == 12
    assert all(item.normalized_sha256 for item in verification.series)


def test_changed_package_copy_fails_before_replay(tmp_path: Path) -> None:
    source = _package_root()
    if not source.is_dir():
        pytest.skip("external accepted G3A package is not mounted")
    copied = tmp_path / "changed-package"
    shutil.copytree(source, copied)
    target = next((copied / "payload").rglob("*.jsonl"))
    target.chmod(stat.S_IWRITE | stat.S_IREAD)
    target.write_bytes(target.read_bytes() + b"\n")

    with pytest.raises(ArtifactIntegrityError, match="payload hash"):
        _verify_package(copied, require_read_only=False)


def test_point_in_time_windows_contain_only_already_closed_candles(
    accepted_dataset: ReplayDataset,
) -> None:
    decision = datetime(2026, 8, 14, 11, 20, tzinfo=UTC)
    for resolution, minutes in RESOLUTION_MINUTES.items():
        window = accepted_dataset.closed_window(
            "CS.D.EURUSD.CEFM.IP",
            resolution,
            decision_time=decision,
            count=60,
        )
        assert len(window) == 60
        assert all(item.timestamp_utc + timedelta(minutes=minutes) <= decision for item in window)
        assert window[-1].timestamp_utc + timedelta(minutes=minutes) <= decision


def test_current_minute_is_never_visible_before_its_close(
    accepted_dataset: ReplayDataset,
) -> None:
    decision = datetime(2026, 8, 14, 11, 19, 30, tzinfo=UTC)
    window = accepted_dataset.closed_window(
        "CS.D.EURUSD.CEFM.IP",
        "MINUTE",
        decision_time=decision,
        count=60,
    )

    assert window[-1].timestamp_utc == datetime(2026, 8, 14, 11, 18, tzinfo=UTC)


def test_initial_warmup_and_gap_rebuild_are_enforced(
    accepted_dataset: ReplayDataset,
    replay_document: dict[str, object],
) -> None:
    metrics = replay_document["metrics"]
    assert metrics["warmup_invalidated_timestamps"] == 292
    assert metrics["gap_invalidated_timestamps"] == 1
    post_gap = AUTHORITATIVE_GAP_TIMESTAMP + timedelta(minutes=1)
    end_decision = datetime(2026, 8, 14, 20, 59, tzinfo=UTC)
    assert (
        len(
            accepted_dataset.closed_window(
                AUTHORITATIVE_GAP_EPIC,
                "MINUTE",
                decision_time=end_decision,
                count=60,
                not_before=post_gap,
            )
        )
        == 60
    )
    assert (
        len(
            accepted_dataset.closed_window(
                AUTHORITATIVE_GAP_EPIC,
                "HOUR",
                decision_time=end_decision,
                count=60,
                not_before=post_gap,
            )
        )
        == 0
    )


def test_authoritative_gap_is_journalled_without_a_candle(
    accepted_dataset: ReplayDataset,
    replay_document: dict[str, object],
) -> None:
    assert (
        accepted_dataset.candle_at(
            AUTHORITATIVE_GAP_EPIC,
            "MINUTE",
            AUTHORITATIVE_GAP_TIMESTAMP,
        )
        is None
    )
    gap = replay_document["gap_policy"]
    assert gap["policy_id"] == "GAP_AWARE_REPLAY_V1"
    assert gap["decisions_prevented"] == 116
    assert gap["events"] == [
        {
            "epic": AUTHORITATIVE_GAP_EPIC,
            "resolution": "MINUTE",
            "timestamp_utc": "2026-08-14T19:03:00+00:00",
            "status": "AUTHORITATIVE_GAP",
            "signal_evaluation": "NO_TRADE",
            "indicator_state": "INVALIDATED",
            "warmup_restart_utc": "2026-08-14T19:04:00+00:00",
        }
    ]


def test_frozen_parameters_and_configuration_hash_are_exact() -> None:
    config = FrozenV1Config()

    assert asdict(config) == {
        "rsi_period": 7,
        "confidence_threshold": 0.70,
        "adx_threshold": 20.0,
        "warmup_candles": 60,
        "stop_atr_multiplier": 2.0,
        "reward_to_risk": 1.5,
        "maximum_stop_pips": 12.0,
        "maximum_spread_pips": 1.2,
        "maximum_spread_to_target_ratio": 0.15,
        "maximum_total_positions": 1,
        "maximum_positions_per_instrument": 1,
        "maximum_executions_per_cycle": 1,
        "scalper_budget_fraction": 0.30,
        "scalper_risk_fraction": 0.005,
        "maximum_daily_loss_fraction": 0.05,
    }
    assert len(frozen_replay_configuration_hash(config)) == 64


def test_accepted_g2_account_state_provenance_hash_and_exact_epic_policy(
    qualification_state: G2QualificationState,
    tmp_path: Path,
) -> None:
    document = qualification_state.document()
    broker = qualification_state.create_paper_broker(tmp_path / "paper-broker.db")
    captured_at = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    snapshot = broker.account_snapshot(as_of=captured_at)

    assert isinstance(broker, PaperBroker)
    assert snapshot is not None
    assert snapshot.state_known
    assert snapshot.captured_at == captured_at
    assert snapshot.balance == snapshot.starting_balance == 10_000.0
    assert snapshot.positions == ()
    assert document["accepted_g2_commit_sha"] == ACCEPTED_G2_COMMIT_SHA
    assert document["fixture_sha256"] == ACCEPTED_G2_FIXTURE_SHA256
    assert document["qualification_account_state_hash"] == (
        "3f180e98de682edae72ea2f96d61b349ba8d0309a6ec9a1bd6a426e510863b84"
    )
    assert qualification_state.pip_value_account_currency("CS.D.GBPUSD.MINI.IP") == 1.0
    assert qualification_state.pip_value_account_currency("CS.D.EURUSD.CEFM.IP") is None


def test_changed_qualification_fixture_is_a_classified_account_state_gap(
    tmp_path: Path,
) -> None:
    changed = tmp_path / "changed-g2-fixture.json"
    changed.write_bytes(
        QUALIFICATION_FIXTURE.read_bytes().replace(
            b'"starting_balance": 10000.0', b'"starting_balance": 9000.0'
        )
    )

    with pytest.raises(QualificationAccountStateGap, match="differs from accepted G2"):
        G2QualificationState.load(changed)


def test_existing_portfolio_risk_remains_the_authoritative_allow_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate(Side.BUY, atr=0.0002, spread_pips=0.4)
    calls: list[tuple[object, int, float]] = []

    def veto(
        self: PortfolioRisk,
        observed: TradeCandidate,
        *,
        account: object,
        executions_in_cycle: int,
        stop_pips: float,
    ) -> RiskDecision:
        del self
        calls.append((account, executions_in_cycle, stop_pips))
        assert observed is candidate
        return RiskDecision(
            False,
            "PORTFOLIO_RISK_SENTINEL",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )

    monkeypatch.setattr(PortfolioRisk, "evaluate", veto)
    account = _account(candidate.quote.timestamp)

    decision = evaluate_candidate(
        candidate,
        config=FrozenV1Config(),
        account=account,
        executions_in_cycle=0,
    )

    assert decision.code == "PORTFOLIO_RISK_SENTINEL"
    assert calls == [(account, 0, 4.0)]


@pytest.mark.parametrize(
    ("atr", "spread_pips", "expected"),
    [
        (0.0007, 0.8, "MAXIMUM_STOP_EXCEEDED"),
        (0.0004, 1.3, "MAXIMUM_SPREAD_EXCEEDED"),
        (0.0001, 1.0, "SPREAD_TARGET_RATIO_EXCEEDED"),
    ],
)
def test_stop_maximum_and_spread_gates(
    atr: float,
    spread_pips: float,
    expected: str,
) -> None:
    candidate = _candidate(Side.BUY, atr=atr, spread_pips=spread_pips)

    decision = evaluate_candidate(
        candidate,
        config=FrozenV1Config(),
        account=_account(candidate.quote.timestamp),
        executions_in_cycle=0,
    )

    assert not decision.allowed
    assert decision.code == expected


def test_unknown_account_and_daily_risk_fail_closed() -> None:
    candidate = _candidate(Side.BUY, atr=0.0004, spread_pips=0.8)
    unknown_daily_risk = AccountSnapshot(
        "A",
        "EUR",
        10_000.0,
        10_000.0,
        (),
        candidate.quote.timestamp,
        False,
    )
    unavailable_daily_risk = AccountSnapshot(
        "A",
        "EUR",
        10_000.0,
        0.0,
        (),
        candidate.quote.timestamp,
        True,
    )

    assert (
        evaluate_candidate(
            candidate,
            config=FrozenV1Config(),
            account=None,
            executions_in_cycle=0,
        ).code
        == "ACCOUNT_STATE_UNKNOWN"
    )
    assert (
        evaluate_candidate(
            candidate,
            config=FrozenV1Config(),
            account=unavailable_daily_risk,
            executions_in_cycle=0,
        ).code
        == "DAILY_RISK_UNKNOWN"
    )
    assert (
        evaluate_candidate(
            candidate,
            config=FrozenV1Config(),
            account=unknown_daily_risk,
            executions_in_cycle=0,
        ).code
        == "ACCOUNT_STATE_UNKNOWN"
    )


def test_one_position_and_one_execution_per_cycle_are_absolute_vetoes() -> None:
    candidate = _candidate(Side.BUY, atr=0.0004, spread_pips=0.8)
    occupied = _account(
        candidate.quote.timestamp,
        positions=(
            Position(
                "P",
                "I",
                candidate.signal.epic,
                Side.BUY,
                0.1,
                1.0,
                0.999,
                1.001,
                0.0001,
                1.0,
                candidate.quote.timestamp,
            ),
        ),
    )

    position_veto = evaluate_candidate(
        candidate,
        config=FrozenV1Config(),
        account=occupied,
        executions_in_cycle=0,
    )
    cycle_veto = evaluate_candidate(
        candidate,
        config=FrozenV1Config(),
        account=_account(candidate.quote.timestamp),
        executions_in_cycle=1,
    )

    assert position_veto.code == "TOTAL_POSITION_LIMIT"
    assert cycle_veto.code == "CYCLE_EXECUTION_LIMIT"


def test_trade_intent_uses_offer_for_long_and_bid_for_short() -> None:
    long_candidate = _candidate(Side.BUY, atr=0.0004, spread_pips=0.8)
    short_candidate = _candidate(Side.SELL, atr=0.0004, spread_pips=0.8)
    config = FrozenV1Config()
    configuration_hash = frozen_replay_configuration_hash(config)
    long_decision = evaluate_candidate(
        long_candidate,
        config=config,
        account=_account(long_candidate.quote.timestamp),
        executions_in_cycle=0,
    )
    short_decision = evaluate_candidate(
        short_candidate,
        config=config,
        account=_account(short_candidate.quote.timestamp),
        executions_in_cycle=0,
    )

    long_intent = build_trade_intent(
        long_candidate,
        long_decision,
        configuration_hash=configuration_hash,
    )
    short_intent = build_trade_intent(
        short_candidate,
        short_decision,
        configuration_hash=configuration_hash,
    )

    assert long_intent.signal_inputs["requested_entry"] == long_candidate.quote.offer
    assert short_intent.signal_inputs["requested_entry"] == short_candidate.quote.bid
    assert long_intent.stop_level < long_candidate.quote.offer < long_intent.target_level
    assert short_intent.target_level < short_candidate.quote.bid < short_intent.stop_level
    assert long_decision.stop_pips == short_decision.stop_pips == 8.0
    assert long_decision.target_pips == short_decision.target_pips == 12.0


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_bid_offer_exit_and_ambiguous_intrabar_are_conservative(side: Side) -> None:
    candidate = _candidate(side, atr=0.0004, spread_pips=0.8)
    config = FrozenV1Config()
    decision = evaluate_candidate(
        candidate,
        config=config,
        account=_account(candidate.quote.timestamp),
        executions_in_cycle=0,
    )
    intent = build_trade_intent(
        candidate,
        decision,
        configuration_hash=frozen_replay_configuration_hash(config),
    )
    candle = _ambiguous_candle(intent.stop_level, intent.target_level, side)

    outcome = resolve_exit(intent, candle)

    assert outcome is not None
    assert outcome.ambiguous_intrabar
    assert outcome.reason == "AMBIGUOUS_INTRABAR_STOP"
    assert outcome.price == intent.stop_level


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_exit_does_not_use_non_executable_side(side: Side) -> None:
    candidate = _candidate(side, atr=0.0004, spread_pips=0.8)
    config = FrozenV1Config()
    decision = evaluate_candidate(
        candidate,
        config=config,
        account=_account(candidate.quote.timestamp),
        executions_in_cycle=0,
    )
    intent = build_trade_intent(
        candidate,
        decision,
        configuration_hash=frozen_replay_configuration_hash(config),
    )
    candle = _non_executable_target_touch(intent.stop_level, intent.target_level, side)

    assert resolve_exit(intent, candle) is None


def test_replay_is_value_equivalent_and_has_expected_signal_rejections(
    accepted_dataset: ReplayDataset,
    replay_document: dict[str, object],
    qualification_state: G2QualificationState,
    tmp_path: Path,
) -> None:
    broker = qualification_state.create_paper_broker(tmp_path / "repeated-broker.db")
    repeated = ExactReplayEngine(
        accepted_dataset,
        commit_sha="0" * 40,
        network_metrics=_zero_network(),
        qualification_state=qualification_state,
        paper_broker=broker,
    ).run()

    assert canonical_bytes(replay_document) == canonical_bytes(repeated)
    assert replay_document["replay_run_fingerprint"] == repeated["replay_run_fingerprint"]
    assert replay_document["metrics"]["decision_timestamps"] == 1917
    assert replay_document["metrics"]["valid_decision_timestamps"] == 1624
    assert replay_document["metrics"]["strategy_signals"] == 20
    assert replay_document["metrics"]["signals"] == {
        "BUY": 13,
        "SELL": 7,
        "NO_TRADE": 1897,
    }
    assert replay_document["metrics"]["rejection_reasons"] == {
        "SPREAD_TARGET_RATIO_EXCEEDED": 10,
        "TOTAL_POSITION_LIMIT": 4,
    }
    assert replay_document["metrics"]["risk_rejections"] == 4
    assert replay_document["metrics"]["spread_rejections"] == 10
    assert replay_document["metrics"]["duplicate_execution_attempts_prevented"] == 2
    assert replay_document["metrics"]["executed_paper_trades"] == 4
    assert replay_document["metrics"]["closed_paper_trades"] == 4
    assert replay_document["performance_evidence_classification"] == (
        "NEGATIVE_ON_AVAILABLE_SAMPLE"
    )
    assert replay_document["final_recommendation"] == "PASS_FOR_G3B_MERGE"


def test_candidate_audit_has_all_twenty_original_candidates_and_exact_dispositions(
    replay_document: dict[str, object],
) -> None:
    audit = replay_document["candidate_audit"]
    expected_fields = {
        "candidate_id",
        "epic",
        "decision_timestamp_utc",
        "side",
        "confidence",
        "spread_pips",
        "target_pips",
        "spread_target_ratio",
        "account_state_result",
        "portfolio_risk_result",
        "final_disposition",
        "intent_id",
    }

    assert len(audit) == 20
    assert len({item["candidate_id"] for item in audit}) == 20
    assert all(expected_fields <= item.keys() for item in audit)
    assert all(
        item["account_state_result"] == "KNOWN_AUTHORITATIVE_G2_PAPER_STATE" for item in audit
    )
    assert replay_document["candidate_disposition_counts"] == {
        "STRATEGY_NO_SIGNAL": 1604,
        "SPREAD_REJECTION": 10,
        "RISK_REJECTION_ACCOUNT_STATE": 0,
        "RISK_REJECTION_OTHER": 4,
        "CYCLE_SUPPRESSED": 2,
        "TRADEINTENT_ACCEPTED": 4,
    }

    spread_rejected = [item for item in audit if item["final_disposition"] == "SPREAD_REJECTION"]
    position_rejected = [
        item for item in audit if item["portfolio_risk_result"] == "TOTAL_POSITION_LIMIT"
    ]
    accepted = [item for item in audit if item["final_disposition"] == "TRADEINTENT_ACCEPTED"]
    assert len(spread_rejected) == 10
    assert all(
        item["portfolio_risk_result"].startswith("NOT_EVALUATED_PRE_PORTFOLIO_GATE")
        for item in spread_rejected
    )
    assert len(position_rejected) == 4
    assert all(item["account_snapshot"]["open_positions"] == 1 for item in position_rejected)
    assert len(accepted) == 4
    assert all(item["portfolio_risk_result"] == "ALLOWED" for item in accepted)
    assert all(item["intent_id"] for item in accepted)


def test_paper_broker_execution_and_negative_limited_performance_are_exact(
    replay_document: dict[str, object],
) -> None:
    metrics = replay_document["metrics"]
    trades = replay_document["trade_execution_audit"]
    accepted_intents = {
        item["intent_id"]
        for item in replay_document["candidate_audit"]
        if item["final_disposition"] == "TRADEINTENT_ACCEPTED"
    }

    assert metrics["paper_broker_fills"] == metrics["closed_paper_trades"] == 4
    assert metrics["wins"] == 0
    assert metrics["losses"] == 4
    assert metrics["net_spread_adjusted_result_pips"] == pytest.approx(-16.0)
    assert metrics["result_r_multiples"] == pytest.approx(-4.0)
    assert metrics["maximum_drawdown_pips"] == pytest.approx(16.0)
    assert metrics["maximum_consecutive_losses"] == 4
    assert metrics["average_holding_duration_seconds"] == 540.0
    assert metrics["profit_loss_account_currency"] == pytest.approx(-59.8)
    assert metrics["open_at_dataset_end"] == 0
    assert len(trades) == 4
    assert {item["intent_id"] for item in trades} == accepted_intents
    assert all(item["reason"] == "STOP" for item in trades)
    assert all(item["net_pips"] == pytest.approx(-4.0) for item in trades)
    assert replay_document["account_and_risk_state"]["final_snapshot"]["balance"] == pytest.approx(
        9_940.2
    )


def test_cli_proves_zero_network_ig_stream_order_and_credentials(tmp_path: Path) -> None:
    package_root = _package_root()
    if not package_root.is_dir():
        pytest.skip("external accepted G3A package is not mounted")
    evidence_json = tmp_path / "g3b.json"
    evidence_markdown = tmp_path / "g3b.md"
    command = [
        sys.executable,
        "-m",
        "src.ig_trader.g3b_replay",
        "--mode",
        "OFFLINE_REPLAY",
        "--package-root",
        str(package_root),
        "--qualification-fixture",
        str(QUALIFICATION_FIXTURE),
        "--state-root",
        str(tmp_path / "paper-state"),
        "--evidence-json",
        str(evidence_json),
        "--evidence-markdown",
        str(evidence_markdown),
    ]
    environment = {**os.environ, "PYTHONPATH": str(ROOT)}
    for name in ("IG_ENVIRONMENT", "EXECUTION_MODE"):
        environment.pop(name, None)
    trading_database = ROOT / "trading.db"
    database_mtime = trading_database.stat().st_mtime_ns if trading_database.exists() else None

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
    network = evidence["network_isolation"]
    for name in (
        "network_call_count",
        "ig_rest_call_count",
        "lightstreamer_connection_count",
        "order_endpoint_call_count",
        "credential_resolution_count",
    ):
        assert network[name] == 0
        assert f"{name}=0" in completed.stdout
    assert evidence["engineering_replay_classification"] == "PASS_REPLAY_INTEGRITY"
    assert evidence["performance_evidence_classification"] == "NEGATIVE_ON_AVAILABLE_SAMPLE"
    assert evidence["final_recommendation"] == "PASS_FOR_G3B_MERGE"
    assert evidence["candidate_disposition_counts"]["RISK_REJECTION_ACCOUNT_STATE"] == 0
    if database_mtime is None:
        assert not trading_database.exists()
    else:
        assert trading_database.stat().st_mtime_ns == database_mtime


def test_cli_stops_before_execution_on_qualification_account_state_gap(
    tmp_path: Path,
) -> None:
    package_root = _package_root()
    if not package_root.is_dir():
        pytest.skip("external accepted G3A package is not mounted")
    changed = tmp_path / "changed-g2-fixture.json"
    changed.write_bytes(QUALIFICATION_FIXTURE.read_bytes() + b"\n")
    evidence_json = tmp_path / "gap.json"
    evidence_markdown = tmp_path / "gap.md"
    state_root = tmp_path / "paper-state"
    command = [
        sys.executable,
        "-m",
        "src.ig_trader.g3b_replay",
        "--mode",
        "OFFLINE_REPLAY",
        "--package-root",
        str(package_root),
        "--qualification-fixture",
        str(changed),
        "--state-root",
        str(state_root),
        "--evidence-json",
        str(evidence_json),
        "--evidence-markdown",
        str(evidence_markdown),
    ]

    completed = subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 8
    assert "reason=QUALIFICATION_ACCOUNT_STATE_GAP" in completed.stderr
    assert not state_root.exists()
    assert not evidence_json.exists()
    assert not evidence_markdown.exists()


def _zero_network() -> dict[str, int]:
    return {
        "network_call_count": 0,
        "ig_rest_call_count": 0,
        "lightstreamer_connection_count": 0,
        "order_endpoint_call_count": 0,
        "credential_resolution_count": 0,
        "blocked_network_attempt_count": 0,
        "blocked_process_attempt_count": 0,
        "blocked_ig_import_attempt_count": 0,
        "blocked_lightstreamer_import_attempt_count": 0,
        "blocked_credential_import_attempt_count": 0,
        "blocked_order_import_attempt_count": 0,
    }


def _candidate(side: Side, *, atr: float, spread_pips: float) -> TradeCandidate:
    timestamp = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    bid = 1.0
    quote = Quote(
        "CS.D.EURGBP.MINI.IP",
        bid,
        bid + spread_pips * 0.0001,
        timestamp,
        0.0001,
        1.0,
        0.1,
        2.0,
    )
    signal = Signal(
        quote.epic,
        side,
        timestamp - timedelta(minutes=1),
        bid,
        "Scalper",
        "Scalper:rsi-adx-v1",
        0.8,
        {"atr": atr},
    )
    return TradeCandidate(
        f"C-{side.value}-{atr}-{spread_pips}",
        timestamp.isoformat(),
        signal,
        quote,
        ({"resolution": "MINUTE", "candle_count": 60},),
        "a" * 64,
    )


def _account(
    captured_at: datetime,
    *,
    positions: tuple[Position, ...] = (),
) -> AccountSnapshot:
    return AccountSnapshot("A", "EUR", 10_000.0, 10_000.0, positions, captured_at, True)


def _ambiguous_candle(stop: float, target: float, side: Side) -> CanonicalCandle:
    if side is Side.BUY:
        bid_low, bid_high = stop - 0.0001, target + 0.0001
        offer_low, offer_high = bid_low + 0.0001, bid_high + 0.0001
    else:
        offer_low, offer_high = target - 0.0001, stop + 0.0001
        bid_low, bid_high = offer_low - 0.0001, offer_high - 0.0001
    bid_open = bid_close = (bid_low + bid_high) / 2.0
    offer_open = offer_close = (offer_low + offer_high) / 2.0
    return CanonicalCandle(
        epic="CS.D.EURGBP.MINI.IP",
        resolution="MINUTE",
        timestamp_utc=datetime(2026, 8, 14, 12, 1, tzinfo=UTC),
        bid_open=bid_open,
        bid_high=bid_high,
        bid_low=bid_low,
        bid_close=bid_close,
        offer_open=offer_open,
        offer_high=offer_high,
        offer_low=offer_low,
        offer_close=offer_close,
        last_traded_open=None,
        last_traded_high=None,
        last_traded_low=None,
        last_traded_close=None,
        volume=1.0,
        source_timestamp=None,
        source_timestamp_utc="2026-08-14T12:01:00",
        source_page=1,
        source_index=1,
        source_raw_file="unit-test",
    )


def _non_executable_target_touch(stop: float, target: float, side: Side) -> CanonicalCandle:
    midpoint = (stop + target) / 2.0
    if side is Side.BUY:
        bid_low = stop + 0.0001
        bid_high = target - 0.00001
        offer_low = bid_low + 0.0001
        offer_high = target + 0.0001
    else:
        offer_low = target + 0.00001
        offer_high = stop - 0.0001
        bid_low = target - 0.0001
        bid_high = offer_high - 0.0001
    return CanonicalCandle(
        epic="CS.D.EURGBP.MINI.IP",
        resolution="MINUTE",
        timestamp_utc=datetime(2026, 8, 14, 12, 1, tzinfo=UTC),
        bid_open=midpoint,
        bid_high=bid_high,
        bid_low=bid_low,
        bid_close=midpoint,
        offer_open=midpoint + 0.0001,
        offer_high=offer_high,
        offer_low=offer_low,
        offer_close=midpoint + 0.0001,
        last_traded_open=None,
        last_traded_high=None,
        last_traded_low=None,
        last_traded_close=None,
        volume=1.0,
        source_timestamp=None,
        source_timestamp_utc="2026-08-14T12:01:00",
        source_page=1,
        source_index=1,
        source_raw_file="unit-test",
    )
