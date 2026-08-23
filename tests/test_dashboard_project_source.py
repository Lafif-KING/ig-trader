from __future__ import annotations

import json
from pathlib import Path

import pytest

from dashboard.sources.project import (
    ProjectGateValidationError,
    load_project_gates,
    load_project_status,
)
from dashboard.status import weighted_progress

REVIEWED_AT = "2026-08-23T14:34:36+00:00"


def test_project_status_reflects_reviewed_database_recovery_hold() -> None:
    project_status = load_project_status()
    summary = project_status.summary
    gates = {gate.gate_id: gate for gate in project_status.gates}

    assert summary.current_phase_gate_id == "DATABASE_RECOVERY"
    assert summary.current_phase == "Real Azure Database Recovery"
    assert summary.current_status == "HOLD"
    assert summary.current_blocker == "The real migration 003 database state is unknown."
    assert summary.real_database_state == "UNKNOWN"
    assert summary.real_database_governance == "RECOVERY_HOLD"
    assert summary.last_verified_at.isoformat() == REVIEWED_AT
    assert gates["DATABASE_RECOVERY"].group == "FOUNDATION"
    assert gates["DATABASE_RECOVERY"].governance_status == "HOLD"
    assert gates["DATABASE_RECOVERY"].technical_status == "UNKNOWN"
    assert gates["DATABASE_RECOVERY"].weight == 8
    assert gates["DATABASE_RECOVERY"].last_verified_at.isoformat() == REVIEWED_AT
    assert gates["DATABASE_RECOVERY"].evidence == (
        "Real migration 003 state: UNKNOWN.",
        "Database mutation: HOLD.",
        "Bootstrap-admin retry: PROHIBITED.",
        "Temporary recovery resources remain retained.",
    )


def test_foundation_progress_is_83_percent_and_authority_is_separate() -> None:
    gates = load_project_gates()
    foundation = tuple(gate for gate in gates if gate.group == "FOUNDATION")
    authority = tuple(gate for gate in gates if gate.group in {"SHADOW", "DEMO", "LIVE"})

    assert weighted_progress(foundation) == 83
    assert weighted_progress(authority, authority=True) == 32
    assert all(
        gate.governance_status == "DISABLED"
        for gate in gates
        if gate.gate_id in {"DEMO_EXECUTION", "LIVE_EXECUTION"}
    )
    dq01_gate = next(gate for gate in gates if gate.gate_id == "DQ01_DEMO_QUALIFICATION_CORE")
    assert dq01_gate.governance_status == dq01_gate.technical_status == "IN_PROGRESS"
    assert dq01_gate.weight == 0


def test_strategy_lab_gate_is_in_progress_and_cannot_change_authority_readiness() -> None:
    gates = {gate.gate_id: gate for gate in load_project_gates()}
    strategy_lab = gates["SL01_MULTI_INSTRUMENT_STRATEGY_LAB"]
    assert strategy_lab.group == "ENHANCEMENTS"
    assert strategy_lab.governance_status == "IN_PROGRESS"
    assert strategy_lab.technical_status == "IN_PROGRESS"
    assert strategy_lab.weight == 0
    authority = tuple(gate for gate in gates.values() if gate.group in {"SHADOW", "DEMO", "LIVE"})
    assert weighted_progress(authority, authority=True) == 32


def test_project_gates_keep_reviewed_links_and_current_verification_dates() -> None:
    gates = {gate.gate_id: gate for gate in load_project_gates()}
    assert gates["G1"].technical_status == "PASS"
    assert gates["G3A"].technical_status == "PASS_WITH_KNOWN_GAP"
    assert gates["G3B"].technical_status == "ENGINEERING_CLOSED"
    assert gates["G3B"].blocker == "Strategy conclusion: INCONCLUSIVE_NEGATIVE_TINY_SAMPLE."
    assert gates["SHADOW_CORE"].related_pr == "#10"
    assert gates["SHADOW_POSTGRES_STORE"].related_pr == "#11"
    assert gates["SHADOW_RUNTIME_V3"].related_pr == "#12"
    assert all(gate.last_verified_at.isoformat() == REVIEWED_AT for gate in gates.values())


def test_project_status_rejects_missing_summary_required_field(tmp_path: Path) -> None:
    source_path = tmp_path / "gates.json"
    schema_path = tmp_path / "gates.schema.json"
    root = Path(__file__).resolve().parents[1]
    source = json.loads((root / "project" / "gates.json").read_text(encoding="utf-8"))
    schema = (root / "project" / "gates.schema.json").read_text(encoding="utf-8")
    del source["project_summary"]["current_blocker"]
    source_path.write_text(json.dumps(source), encoding="utf-8")
    schema_path.write_text(schema, encoding="utf-8")

    with pytest.raises(ProjectGateValidationError, match="does not match its JSON schema"):
        load_project_status(source_path)


def test_gate_source_rejects_missing_required_field(tmp_path: Path) -> None:
    source_path = tmp_path / "gates.json"
    schema_path = tmp_path / "gates.schema.json"
    root = Path(__file__).resolve().parents[1]
    source = json.loads((root / "project" / "gates.json").read_text(encoding="utf-8"))
    schema = (root / "project" / "gates.schema.json").read_text(encoding="utf-8")
    del source["gates"][0]["next_action"]
    source_path.write_text(json.dumps(source), encoding="utf-8")
    schema_path.write_text(schema, encoding="utf-8")

    with pytest.raises(ProjectGateValidationError, match="does not match its JSON schema"):
        load_project_gates(source_path)
