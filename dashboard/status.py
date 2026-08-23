"""Status labels and deliberately conservative progress calculations."""

from __future__ import annotations

from dashboard.models import ProjectGate

STATUS_LABELS = {
    "PASS": "PASS",
    "PASS_WITH_KNOWN_GAP": "PASS — KNOWN GAP",
    "ENGINEERING_CLOSED": "ENGINEERING CLOSED",
    "IN_PROGRESS": "IN PROGRESS",
    "HOLD": "HOLD",
    "BLOCKED": "BLOCKED",
    "NOT_STARTED": "NOT STARTED",
    "DISABLED": "DISABLED",
    "NOT_AUTHORIZED": "NOT AUTHORIZED",
    "UNKNOWN": "UNKNOWN",
}

COMPLETED_TECHNICAL_STATUSES = {"PASS", "PASS_WITH_KNOWN_GAP", "ENGINEERING_CLOSED"}
READY_AUTHORITY_STATUSES = {"PASS"}


def display_status(status: str) -> str:
    """Return an accessible label rather than relying on colour alone."""

    return STATUS_LABELS.get(status, "UNKNOWN")


def weighted_progress(gates: tuple[ProjectGate, ...], *, authority: bool = False) -> int:
    """Calculate scoped progress without treating disabled gates as complete."""

    scoped_gates = tuple(gate for gate in gates if gate.weight > 0)
    total_weight = sum(gate.weight for gate in scoped_gates)
    if total_weight == 0:
        return 0
    if authority:
        completed_weight = sum(
            gate.weight
            for gate in scoped_gates
            if gate.governance_status in READY_AUTHORITY_STATUSES
        )
    else:
        completed_weight = sum(
            gate.weight
            for gate in scoped_gates
            if gate.technical_status in COMPLETED_TECHNICAL_STATUSES
        )
    return round(completed_weight / total_weight * 100)


def gates_for_group(gates: tuple[ProjectGate, ...], group: str) -> tuple[ProjectGate, ...]:
    """Keep roadmap grouping deterministic and independent of live GitHub data."""

    return tuple(gate for gate in gates if gate.group == group)
