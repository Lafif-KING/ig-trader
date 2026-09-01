from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import src.ig_trader.shadow01.clock as clock_module
from src.ig_trader.shadow01.clock import (
    ClockAvailability,
    ShadowClockError,
    assess_universal_clock,
    require_decision_anchor,
)
from src.ig_trader.shadow01.config import ShadowConfigError, ShadowTournamentConfig, load_config
from src.ig_trader.shadow01.models import (
    AssetClass,
    ContextState,
    CostState,
    DailyBar,
    Direction,
    FundamentalState,
    MarketSnapshot,
    OutcomeLabel,
    PolicyId,
    QualityAssessment,
    QualityState,
    ShadowDecision,
)
from src.ig_trader.shadow01.outcomes import OutcomeResolutionInput, resolve_outcomes
from src.ig_trader.shadow01.registry import load_verified_dq03_registry
from src.ig_trader.shadow01.storage import ShadowStoreError, ShadowTournamentStore
from tests.shadow01_dq03_fixtures import verified_dq03_documents, write_verified_dq03_documents

NOW = datetime(2026, 9, 2, 21, 10, tzinfo=UTC)


def _decision(config: ShadowTournamentConfig, timestamp: datetime = NOW) -> ShadowDecision:
    return ShadowDecision(
        decision_id="shadow01-test-decision",
        tournament_version=config.version,
        config_fingerprint=config.fingerprint,
        decision_timestamp_utc=timestamp,
        instrument="EURUSD",
        epic="TEST.EURUSD",
        policy_id=PolicyId.P0_TECHNICAL_TREND_ONLY,
        direction=Direction.LONG,
        technical_engine="T1",
        technical_score=1.0,
        cross_asset_state=ContextState.UNKNOWN,
        fundamental_context=FundamentalState.UNKNOWN,
        quality_state=QualityState.UNKNOWN,
        cost_state=CostState.COST_UNKNOWN,
        factor_tags=("EUR_LONG", "USD_SHORT"),
        reason_codes=("TEST",),
        input_data_fingerprint="input-fingerprint",
        created_at=timestamp,
    )


def _snapshot(timestamp: datetime = NOW) -> MarketSnapshot:
    return MarketSnapshot(
        decision_timestamp_utc=timestamp,
        instrument="EURUSD",
        epic="TEST.EURUSD",
        asset_class=AssetClass.FX,
        completed_bars=(
            DailyBar(
                completed_at=timestamp - timedelta(days=1),
                open=1.0,
                high=1.1,
                low=0.9,
                close=1.05,
            ),
        ),
        metadata={"source": "IG_READ_ONLY"},
        data_quality=QualityAssessment(QualityState.NORMAL, 60, ("TEST",)),
        input_data_fingerprint="snapshot-input-fingerprint",
    )


def _record_epoch_readiness(store: ShadowTournamentStore, config: ShadowTournamentConfig) -> None:
    store.append_provider_health(
        observed_at=NOW,
        provider="IG_READ_ONLY",
        status="HEALTHY",
        detail="SHADOW01_READ_ONLY_CLOCK_PROBE_OK",
        data={"asset_classes": ["FX", "METAL", "INDEX"]},
    )
    store.record_epoch_readiness(
        config,
        snapshot=_snapshot(),
        provider_probe_observed_at=NOW,
    )


def test_registry_never_invents_an_epic_when_evidence_is_missing(tmp_path: Path) -> None:
    config = load_config()

    absent = load_verified_dq03_registry(config, None)

    assert len(absent.markets) == 20
    assert absent.verified_count == 0
    assert {item.epic for item in absent.markets} == {None}
    assert {item.reason for item in absent.markets} == {"DQ03_REGISTRY_NOT_SUPPLIED"}

    write_verified_dq03_documents(tmp_path, config)
    path = tmp_path / "instrument_registry.json"
    verified = load_verified_dq03_registry(config, path)

    assert verified.verified_count == 20
    assert verified.by_symbol("EURUSD").epic == "TEST.EURUSD"


def test_registry_rejects_a_bare_self_asserted_dq03_json_even_with_epics(tmp_path: Path) -> None:
    config = load_config()
    documents = verified_dq03_documents(config)
    registry = documents["instrument_registry.json"]
    assert isinstance(registry, dict)
    registry.pop("phase")
    registry.pop("latest_augmentation_phase")
    registry.pop("run_context")
    path = tmp_path / "instrument_registry.json"
    path.write_text(json.dumps(registry), encoding="utf-8")

    result = load_verified_dq03_registry(config, path)

    assert result.verified_count == 0
    assert {item.reason for item in result.markets} == {"DQ03_REGISTRY_PROVENANCE_INVALID"}


def test_registry_requires_history_fingerprint_linkage_for_each_epic(tmp_path: Path) -> None:
    config = load_config()
    documents = write_verified_dq03_documents(tmp_path, config)
    history = documents["history_validation.json"]
    samples = history["samples"]
    assert isinstance(samples, list)
    first = samples[0]
    assert isinstance(first, dict)
    first["source_fingerprint"] = "0" * 64
    (tmp_path / "history_validation.json").write_text(json.dumps(history), encoding="utf-8")

    result = load_verified_dq03_registry(config, tmp_path / "instrument_registry.json")

    assert result.verified_count == 19
    assert result.by_symbol("EURUSD").epic is None
    assert result.by_symbol("EURUSD").reason == "DQ03_HISTORY_PROVENANCE_INVALID"


def test_registry_accepts_authoritative_dq03_redacted_candidate_records(tmp_path: Path) -> None:
    """Phase-3 evidence can redact candidates only with an exact history link."""

    config = load_config()
    documents = write_verified_dq03_documents(tmp_path, config)
    registry = documents["instrument_registry.json"]
    history = documents["history_validation.json"]
    instruments = registry["instruments"]
    samples = history["samples"]
    assert isinstance(instruments, list)
    assert isinstance(samples, list)
    samples_by_symbol = {sample["symbol"]: sample for sample in samples if isinstance(sample, dict)}

    for entry in instruments:
        assert isinstance(entry, dict)
        symbol = entry["canonical_symbol"]
        assert isinstance(symbol, str)
        metadata = entry["metadata"]
        assert isinstance(metadata, dict)
        display_name = metadata["display_name"]
        assert isinstance(display_name, str)
        entry["selected_candidate_name"] = display_name
        entry["display_name"] = display_name
        entry["candidates"] = []
        entry["rejected_candidates"] = []
        entry["broker_validation"] = samples_by_symbol[symbol]
    (tmp_path / "instrument_registry.json").write_text(json.dumps(registry), encoding="utf-8")

    result = load_verified_dq03_registry(config, tmp_path / "instrument_registry.json")

    assert result.verified_count == 20
    assert result.unavailable_count == 0


def test_registry_rejects_redacted_candidates_without_exact_history_link(tmp_path: Path) -> None:
    config = load_config()
    documents = write_verified_dq03_documents(tmp_path, config)
    registry = documents["instrument_registry.json"]
    history = documents["history_validation.json"]
    instruments = registry["instruments"]
    samples = history["samples"]
    assert isinstance(instruments, list)
    assert isinstance(samples, list)
    samples_by_symbol = {sample["symbol"]: sample for sample in samples if isinstance(sample, dict)}

    for entry in instruments:
        assert isinstance(entry, dict)
        symbol = entry["canonical_symbol"]
        assert isinstance(symbol, str)
        metadata = entry["metadata"]
        assert isinstance(metadata, dict)
        display_name = metadata["display_name"]
        assert isinstance(display_name, str)
        entry["selected_candidate_name"] = display_name
        entry["display_name"] = display_name
        entry["candidates"] = []
        entry["rejected_candidates"] = []
        entry["broker_validation"] = samples_by_symbol[symbol]
    first = instruments[0]
    assert isinstance(first, dict)
    embedded_history = first["broker_validation"]
    assert isinstance(embedded_history, dict)
    first["broker_validation"] = {**embedded_history, "epic": "TEST.WRONG_EPIC"}
    (tmp_path / "instrument_registry.json").write_text(json.dumps(registry), encoding="utf-8")

    result = load_verified_dq03_registry(config, tmp_path / "instrument_registry.json")

    assert result.verified_count == 19
    assert result.by_symbol("EURUSD").epic is None
    assert result.by_symbol("EURUSD").reason == "DQ03_SELECTION_PROVENANCE_INVALID"


def test_frozen_config_fingerprint_covers_the_exact_engine_formula(tmp_path: Path) -> None:
    config = load_config()
    changed = json.loads(json.dumps(config.payload))
    technical = changed["technical"]
    assert isinstance(technical, dict)
    technical["trend_formula"] = "a changed formula"
    path = tmp_path / "changed-config.json"
    path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(ShadowConfigError, match="T1_FORMULA_NOT_FROZEN"):
        load_config(path)


def test_config_payload_cannot_be_mutated_after_its_fingerprint_is_created() -> None:
    config = load_config()
    mutable_copy = config.payload
    policies = mutable_copy["policies"]
    assert isinstance(policies, dict)
    policies["trend_minimum_strength"] = 999

    fresh_copy = config.payload
    fresh_policies = fresh_copy["policies"]
    assert isinstance(fresh_policies, dict)
    assert fresh_policies["trend_minimum_strength"] != 999
    assert config.fingerprint_is_valid


def test_registry_rejects_unsafe_dq03_authority_claim(tmp_path: Path) -> None:
    config = load_config()
    document = verified_dq03_documents(config)["instrument_registry.json"]
    document["execution_authority"] = "ON"
    write_verified_dq03_documents(tmp_path, config)
    path = tmp_path / "instrument_registry.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    result = load_verified_dq03_registry(config, path)

    assert result.verified_count == 0
    assert {item.reason for item in result.markets} == {"DQ03_REGISTRY_EXECUTION_AUTHORITY_INVALID"}


def test_no_decision_before_epoch_and_epoch_is_immutable(tmp_path: Path) -> None:
    config = load_config()
    store = ShadowTournamentStore(tmp_path / "shadow.sqlite3")
    store.register_version(config, now=NOW)

    with pytest.raises(ShadowStoreError, match="SHADOW01_NO_RETROSPECTIVE_DECISIONS"):
        store.append_decision(_decision(config))
    with pytest.raises(ShadowStoreError, match="HUMAN_AUTHORIZATION_REQUIRED"):
        store.create_epoch(config, epoch_utc=NOW, authorization_phrase="not authorized")

    with pytest.raises(ShadowStoreError, match="EPOCH_READINESS_REQUIRED"):
        store.create_epoch(
            config,
            epoch_utc=NOW,
            authorization_phrase="START SHADOW01-V1 EPOCH",
        )

    _record_epoch_readiness(store, config)

    store.create_epoch(
        config,
        epoch_utc=NOW,
        authorization_phrase="START SHADOW01-V1 EPOCH",
    )
    assert store.epoch(config) == NOW
    store.append_decision(_decision(config))

    with pytest.raises(ShadowStoreError, match="EPOCH_ALREADY_EXISTS"):
        store.create_epoch(
            config,
            epoch_utc=NOW + timedelta(days=1),
            authorization_phrase="START SHADOW01-V1 EPOCH",
        )
    with sqlite3.connect(store.path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE shadow_decisions SET direction = 'SHORT'")


def test_config_fingerprint_is_immutable_and_new_version_needs_new_epoch(tmp_path: Path) -> None:
    config = load_config()
    store = ShadowTournamentStore(tmp_path / "shadow.sqlite3")
    store.register_version(config, now=NOW)

    conflicted = ShadowTournamentConfig(config.payload, "different-fingerprint")
    with pytest.raises(ShadowStoreError, match="CONFIG_FINGERPRINT_INVALID"):
        store.register_version(conflicted, now=NOW)

    v2_payload = {**config.payload, "tournament_version": "SHADOW01-V2"}
    v2 = ShadowTournamentConfig.verified(v2_payload)
    store.register_version(v2, now=NOW)
    decision = replace(
        _decision(config),
        tournament_version="SHADOW01-V2",
        config_fingerprint=v2.fingerprint,
    )
    with pytest.raises(ShadowStoreError, match="NO_RETROSPECTIVE_DECISIONS"):
        store.append_decision(decision)


def test_outcomes_are_separate_append_only_future_labels(tmp_path: Path) -> None:
    config = load_config()
    store = ShadowTournamentStore(tmp_path / "shadow.sqlite3")
    _record_epoch_readiness(store, config)
    store.create_epoch(
        config,
        epoch_utc=NOW,
        authorization_phrase="START SHADOW01-V1 EPOCH",
    )
    decision = _decision(config)
    store.append_decision(decision)
    blocked = OutcomeLabel(
        decision_id=decision.decision_id,
        horizon_sessions=1,
        reference_entry_price=1.0,
        future_price=None,
        raw_directional_return=None,
        atr_normalized_return=None,
        cost_adjusted_result=None,
        outcome_timestamp_utc=None,
        quality=QualityState.BLOCKED,
        blocked_reason="OUTCOME_COMPLETED_SESSION_UNAVAILABLE",
    )
    store.append_outcome(blocked)
    with pytest.raises(sqlite3.IntegrityError):
        store.append_outcome(blocked)

    outcomes = resolve_outcomes(
        OutcomeResolutionInput(decision, 1.0, 0.01, future_completed_bars=())
    )
    assert {item.horizon_sessions for item in outcomes} == {1, 3, 5, 10, 20}
    assert all(item.quality is QualityState.BLOCKED for item in outcomes)

    feature_source = (Path(__file__).parents[1] / "src/ig_trader/shadow01/features.py").read_text(
        encoding="utf-8"
    )
    assert "shadow01.outcomes" not in feature_source
    assert "OutcomeLabel" not in feature_source


def test_clock_is_dst_aware_and_refuses_unproven_universal_availability() -> None:
    config = load_config()
    assert require_decision_anchor(config, NOW) == NOW
    winter_anchor = datetime(2026, 1, 5, 22, 10, tzinfo=UTC)
    assert require_decision_anchor(config, winter_anchor) == winter_anchor

    status, blockers = assess_universal_clock(
        (
            ClockAvailability("FX", True, True),
            ClockAvailability("METAL", True, True),
            ClockAvailability("INDEX", False, False),
        )
    )
    assert status == "SHADOW01_SESSION_CLOCK_HUMAN_GATE_REQUIRED"
    assert blockers == ("INDEX",)


def test_clock_fallback_matches_modern_dst_transitions_and_fails_closed_before_2007(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config()
    monkeypatch.setattr(clock_module, "NEW_YORK", None)

    spring_anchor = datetime(2026, 3, 8, 21, 10, tzinfo=UTC)
    fall_anchor = datetime(2026, 11, 1, 22, 10, tzinfo=UTC)

    assert require_decision_anchor(config, spring_anchor) == spring_anchor
    assert require_decision_anchor(config, fall_anchor) == fall_anchor
    with pytest.raises(ShadowClockError, match="FROZEN_ANCHOR"):
        require_decision_anchor(config, datetime(2026, 3, 8, 22, 10, tzinfo=UTC))
    with pytest.raises(ShadowClockError, match="FROZEN_ANCHOR"):
        require_decision_anchor(config, datetime(2026, 11, 1, 21, 10, tzinfo=UTC))
    with pytest.raises(ShadowClockError, match="TIMEZONE_DATA_REQUIRED"):
        require_decision_anchor(config, datetime(2006, 1, 3, 22, 10, tzinfo=UTC))


def test_atomic_market_observation_rolls_back_all_rows_on_late_constraint_failure(
    tmp_path: Path,
) -> None:
    config = load_config()
    store = ShadowTournamentStore(tmp_path / "shadow.sqlite3")
    _record_epoch_readiness(store, config)
    store.create_epoch(
        config,
        epoch_utc=NOW,
        authorization_phrase="START SHADOW01-V1 EPOCH",
    )
    store.append_decision(_decision(config))
    next_anchor = NOW + timedelta(days=1)
    base = replace(
        _decision(config),
        decision_timestamp_utc=next_anchor,
        created_at=next_anchor,
    )
    decisions = (
        base,
        replace(
            base,
            decision_id="shadow01-next-p1",
            policy_id=PolicyId.P1_TECHNICAL_REVERSION_ONLY,
            technical_engine="M1",
        ),
        replace(
            base,
            decision_id="shadow01-next-p2",
            policy_id=PolicyId.P2_TREND_PLUS_CROSS_ASSET,
            cross_asset_state=ContextState.NEUTRAL,
        ),
        replace(
            base,
            decision_id="shadow01-next-p3",
            policy_id=PolicyId.P3_CONSERVATIVE_CONTEXT,
        ),
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.append_market_observation(
            config,
            decision_timestamp_utc=next_anchor,
            instrument="EURUSD",
            epic="TEST.EURUSD",
            snapshot_data={"test": "atomic-bundle"},
            input_data_fingerprint="input-fingerprint",
            engine_insights={
                engine_id: {"engine_id": engine_id}
                for engine_id in ("TECHNICAL_STATE", "T1", "M1", "X1", "F1", "Q1", "C1")
            },
            decisions=decisions,
        )

    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute(
                """
            SELECT count(*) FROM market_snapshots
            WHERE tournament_version = ? AND decision_timestamp_utc = ? AND instrument = 'EURUSD'
            """,
                (config.version, next_anchor.isoformat()),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """
            SELECT count(*) FROM engine_insights
            WHERE tournament_version = ? AND decision_timestamp_utc = ? AND instrument = 'EURUSD'
            """,
                (config.version, next_anchor.isoformat()),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """
            SELECT count(*) FROM shadow_decisions
            WHERE tournament_version = ? AND decision_timestamp_utc = ? AND instrument = 'EURUSD'
            """,
                (config.version, next_anchor.isoformat()),
            ).fetchone()[0]
            == 0
        )


def test_storage_requires_anchor_and_keeps_runs_and_provider_health_append_only(
    tmp_path: Path,
) -> None:
    config = load_config()
    store = ShadowTournamentStore(tmp_path / "shadow.sqlite3")
    _record_epoch_readiness(store, config)
    store.create_epoch(
        config,
        epoch_utc=NOW,
        authorization_phrase="START SHADOW01-V1 EPOCH",
    )
    unanchored = NOW + timedelta(minutes=1)

    with pytest.raises(ShadowStoreError, match="FROZEN_ANCHOR"):
        store.append_snapshot(
            config,
            decision_timestamp_utc=unanchored,
            instrument="EURUSD",
            epic="TEST.EURUSD",
            snapshot_data={"test": "snapshot"},
            input_data_fingerprint="input-fingerprint",
        )
    with pytest.raises(ShadowStoreError, match="FROZEN_ANCHOR"):
        store.append_engine_insight(
            config,
            decision_timestamp_utc=unanchored,
            instrument="EURUSD",
            engine_id="T1",
            insight={"test": "insight"},
        )
    with pytest.raises(ShadowStoreError, match="FROZEN_ANCHOR"):
        store.append_decision(
            replace(
                _decision(config),
                decision_id="shadow01-unanchored-decision",
                decision_timestamp_utc=unanchored,
                created_at=unanchored,
            )
        )

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE tournament_runs SET created_at_utc = created_at_utc")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE tournament_runs SET config_json = '{}' ")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM tournament_runs")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE provider_health SET status = 'MUTATED'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM provider_health")


def test_dashboard_includes_engine_insights_and_granular_coverage(tmp_path: Path) -> None:
    config = load_config()
    store = ShadowTournamentStore(tmp_path / "shadow.sqlite3")
    _record_epoch_readiness(store, config)
    store.create_epoch(
        config,
        epoch_utc=NOW,
        authorization_phrase="START SHADOW01-V1 EPOCH",
    )
    store.append_snapshot(
        config,
        decision_timestamp_utc=NOW,
        instrument="EURUSD",
        epic="TEST.EURUSD",
        snapshot_data={"source": "IG_READ_ONLY"},
        input_data_fingerprint="input-fingerprint",
    )
    store.append_engine_insight(
        config,
        decision_timestamp_utc=NOW,
        instrument="EURUSD",
        engine_id="T1",
        insight={"direction": "LONG", "reason": "TEST"},
    )
    decision = _decision(config)
    store.append_decision(decision)
    store.append_outcome(
        OutcomeLabel(
            decision_id=decision.decision_id,
            horizon_sessions=1,
            reference_entry_price=1.0,
            future_price=1.1,
            raw_directional_return=0.1,
            atr_normalized_return=1.0,
            cost_adjusted_result=None,
            outcome_timestamp_utc=NOW + timedelta(days=1),
            quality=QualityState.NORMAL,
            blocked_reason=None,
        )
    )

    dashboard = store.dashboard_document(config)

    assert dashboard["available"] is True
    assert dashboard["engine_insights"] == [
        {
            "decision_timestamp_utc": NOW.isoformat(),
            "instrument": "EURUSD",
            "engine_id": "T1",
            "insight": {"direction": "LONG", "reason": "TEST"},
        }
    ]
    assert dashboard["resolved_outcomes"] == [
        {
            "decision_timestamp_utc": NOW.isoformat(),
            "instrument": "EURUSD",
            "policy_id": "P0_TECHNICAL_TREND_ONLY",
            "technical_engine": "T1",
            "horizon_sessions": 1,
            "outcome_timestamp_utc": (NOW + timedelta(days=1)).isoformat(),
            "quality": "NORMAL",
            "reference_entry_price": 1.0,
            "future_price": 1.1,
            "raw_directional_return": 0.1,
            "atr_normalized_return": 1.0,
            "cost_adjusted_result": None,
            "blocked_reason": None,
        }
    ]
    row = next(
        item
        for item in dashboard["leaderboard"]
        if item["policy_id"] == "P0_TECHNICAL_TREND_ONLY"
        and item["technical_engine"] == "T1"
        and item["instrument"] == "EURUSD"
        and item["horizon_sessions"] == 1
    )
    assert row["asset_class"] == "FX"
    assert row["decision_count"] == 1
    assert row["directional_decision_count"] == 1
    assert row["abstention_count"] == 0
    assert row["label_coverage"] == 1.0
    assert row["resolved_coverage"] == 1.0


def test_storage_rechecks_mutated_decision_contract_before_append(tmp_path: Path) -> None:
    config = load_config()
    store = ShadowTournamentStore(tmp_path / "shadow.sqlite3")
    _record_epoch_readiness(store, config)
    store.create_epoch(
        config,
        epoch_utc=NOW,
        authorization_phrase="START SHADOW01-V1 EPOCH",
    )

    wrong_engine = _decision(config)
    object.__setattr__(wrong_engine, "technical_engine", "M1")
    with pytest.raises(ShadowStoreError, match="POLICY_ENGINE_INVALID"):
        store.append_decision(wrong_engine)

    quality_bypassed = _decision(config)
    object.__setattr__(quality_bypassed, "quality_state", QualityState.BLOCKED)
    with pytest.raises(ShadowStoreError, match="QUALITY_BLOCK_REQUIRED"):
        store.append_decision(quality_bypassed)

    p2_bypassed = _decision(config)
    object.__setattr__(p2_bypassed, "policy_id", PolicyId.P2_TREND_PLUS_CROSS_ASSET)
    with pytest.raises(ShadowStoreError, match="P2_CONTEXT_BLOCK_REQUIRED"):
        store.append_decision(p2_bypassed)


def test_persistence_rejects_noncanonical_config_without_creating_a_database(
    tmp_path: Path,
) -> None:
    config = load_config()
    invalid = ShadowTournamentConfig(config.payload, "not-the-canonical-fingerprint")
    store = ShadowTournamentStore(tmp_path / "shadow.sqlite3")

    with pytest.raises(ShadowStoreError, match="CONFIG_FINGERPRINT_INVALID"):
        store.register_version(invalid, now=NOW)

    dashboard = store.dashboard_document(invalid)
    assert dashboard["available"] is False
    assert dashboard["reason"] == "SHADOW01_CONFIG_FINGERPRINT_INVALID"
    assert store.path.exists() is False
