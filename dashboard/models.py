"""Small immutable models used by the read-only dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

GateStatus = Literal[
    "PASS",
    "PASS_WITH_KNOWN_GAP",
    "ENGINEERING_CLOSED",
    "IN_PROGRESS",
    "HOLD",
    "BLOCKED",
    "NOT_STARTED",
    "DISABLED",
    "NOT_AUTHORIZED",
    "UNKNOWN",
]


@dataclass(frozen=True)
class ProjectGate:
    """A reviewed project gate; never an execution authority."""

    gate_id: str
    display_name: str
    group: str
    plain_english_description: str
    governance_status: GateStatus
    technical_status: GateStatus
    weight: int
    owner: str
    completed_sha: str | None
    related_pr: str | None
    blocker: str | None
    next_action: str
    evidence: tuple[str, ...]
    last_verified_at: datetime


@dataclass(frozen=True)
class ProjectSummary:
    """Reviewed top-level status that cannot be changed by GitHub evidence."""

    current_phase_gate_id: str
    current_phase: str
    current_status: GateStatus
    current_blocker: str
    next_action: str
    execution_mode: str
    broker_order_authority: str
    demo_execution: GateStatus
    live_execution: GateStatus
    real_database_state: GateStatus
    real_database_governance: str
    last_verified_at: datetime


@dataclass(frozen=True)
class ProjectStatus:
    """Single read-only project source response for pages and calculations."""

    summary: ProjectSummary
    gates: tuple[ProjectGate, ...]


@dataclass(frozen=True)
class PullRequest:
    """Safe, public pull-request metadata."""

    number: int
    title: str
    state: str
    url: str
    head_sha: str | None = None
    merged_at: str | None = None


@dataclass(frozen=True)
class WorkflowRun:
    """Sanitized metadata from the latest GitHub Actions workflow run."""

    name: str
    number: int
    status: str
    conclusion: str | None
    head_sha: str
    branch: str
    url: str
    started_at: str | None
    completed_at: str | None
    passed_steps: int = 0
    failed_steps: int = 0
    skipped_steps: int = 0
    pull_request: int | None = None
    first_failed_step: str | None = None
    failure_summary: str | None = None

    @property
    def display_result(self) -> str:
        if self.status != "completed":
            return "IN PROGRESS"
        if self.conclusion == "success":
            return "PASS"
        if self.conclusion in {"failure", "cancelled", "timed_out", "action_required"}:
            return "FAIL"
        return "UNKNOWN"


@dataclass(frozen=True)
class GitHubStatus:
    """A bounded read-only snapshot of public GitHub metadata."""

    available: bool
    main_sha: str | None = None
    main_updated_at: str | None = None
    open_pull_requests: tuple[PullRequest, ...] = ()
    merged_pull_requests: tuple[PullRequest, ...] = ()
    latest_workflow: WorkflowRun | None = None
    workflow_context: str = "MAIN"


@dataclass(frozen=True)
class ShadowDataStatus:
    """Future broker-neutral Shadow evidence interface response."""

    status: Literal["DATA_NOT_AVAILABLE"]
    reason: str
