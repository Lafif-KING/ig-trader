from __future__ import annotations

from dashboard.sources.project import load_project_gates
from dashboard.status import display_status, weighted_progress


def test_workflow_status_and_accessible_labels_are_conservative() -> None:
    assert display_status("DISABLED") == "DISABLED"
    assert display_status("unrecognized") == "UNKNOWN"


def test_progress_never_counts_disabled_gate_as_complete() -> None:
    gates = load_project_gates()
    authority_gates = tuple(gate for gate in gates if gate.group in {"SHADOW", "DEMO", "LIVE"})
    assert weighted_progress(authority_gates, authority=True) < 100
    assert {gate.gate_id for gate in gates} >= {"DEMO_EXECUTION", "LIVE_EXECUTION"}
