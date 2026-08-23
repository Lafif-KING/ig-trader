from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from dashboard.models import PullRequest
from dashboard.sources.project import load_project_gates, load_project_status
from dashboard.status import display_status, weighted_progress


def test_workflow_status_and_accessible_labels_are_conservative() -> None:
    assert display_status("DISABLED") == "DISABLED"
    assert display_status("unrecognized") == "UNKNOWN"


def test_progress_never_counts_disabled_gate_as_complete() -> None:
    gates = load_project_gates()
    authority_gates = tuple(gate for gate in gates if gate.group in {"SHADOW", "DEMO", "LIVE"})
    assert weighted_progress(authority_gates, authority=True) < 100
    assert {gate.gate_id for gate in gates} >= {"DEMO_EXECUTION", "LIVE_EXECUTION"}


def test_project_status_and_pull_request_models_are_immutable() -> None:
    project_status = load_project_status()
    pull_request = PullRequest(7, "Dashboard", "OPEN", "https://example.test", "a" * 40)
    assert project_status.summary.current_phase_gate_id == "DATABASE_RECOVERY"
    assert pull_request.head_sha == "a" * 40
    with pytest.raises(FrozenInstanceError):
        project_status.summary.current_phase = "Unsafe override"
    with pytest.raises(FrozenInstanceError):
        pull_request.head_sha = "b" * 40
