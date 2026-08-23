from __future__ import annotations

import json
from pathlib import Path

import pytest

from dashboard.sources.project import ProjectGateValidationError, load_project_gates


def test_project_gates_reflect_reviewed_governance_state() -> None:
    gates = {gate.gate_id: gate for gate in load_project_gates()}
    assert gates["G1"].technical_status == "PASS"
    assert gates["G3A"].technical_status == "PASS_WITH_KNOWN_GAP"
    assert gates["G3B"].technical_status == "ENGINEERING_CLOSED"
    assert gates["G3B"].blocker == "Strategy conclusion: INCONCLUSIVE_NEGATIVE_TINY_SAMPLE."
    assert gates["SHADOW_RUNTIME_V3"].completed_sha == "89166115b1f72b8bed32eb2ddee847615b19b413"
    assert gates["DEMO_EXECUTION"].governance_status == "DISABLED"
    assert gates["LIVE_EXECUTION"].governance_status == "DISABLED"


def test_gate_source_rejects_missing_required_field(tmp_path) -> None:
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
