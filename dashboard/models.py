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


# Control Center operator-state contracts.  These are presentation models only:
# they cannot carry a client, an execution permit, or an order request.
OperatorStatus = Literal["NORMAL", "WARNING", "BLOCKED", "UNKNOWN"]


@dataclass(frozen=True)
class StartGate:
    """The rendered result of independently checked start prerequisites."""

    enabled: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class RobotState:
    environment: str
    state: str
    execution_authority: str
    kill_switch: str
    singleton_status: OperatorStatus
    last_decision: str


@dataclass(frozen=True)
class BrokerState:
    rest_status: str
    streaming_status: str
    account_status: str
    last_successful_read: str
    source_label: str


@dataclass(frozen=True)
class InstrumentState:
    instrument: str
    epic: str
    asset_class: str
    market_status: str
    bid: str
    ask: str
    spread: str
    data_freshness: str
    streaming: str
    research_status: str
    approved_strategy: str
    strategy_status: str
    signal: str
    block_reason: str


@dataclass(frozen=True)
class StrategyState:
    strategy_id: str
    family: str
    instrument: str
    timeframe: str
    historical_status: str
    demo_approval: str
    execution_authority: str
    trade_count: str
    oos_expectancy: str
    walk_forward_expectancy: str
    profit_factor: str
    max_drawdown: str
    stress_status: str
    reason: str
    source_label: str


@dataclass(frozen=True)
class DecisionState:
    timestamp: str
    instrument: str
    outcome: str
    primary_reason: str
    market_tradeable: str = "NOT AVAILABLE"
    data_fresh: str = "NOT AVAILABLE"
    strategy_qualified: str = "NOT AVAILABLE"
    strategy_approved: str = "NOT AVAILABLE"
    signal_detected: str = "NOT AVAILABLE"
    opportunity_acceptable: str = "NOT AVAILABLE"
    spread_acceptable: str = "NOT AVAILABLE"
    risk_available: str = "NOT AVAILABLE"
    portfolio_exposure: str = "NOT AVAILABLE"
    kill_switch_released: str = "NOT AVAILABLE"
    execution_authority: str = "NOT AVAILABLE"


@dataclass(frozen=True)
class PositionState:
    instrument: str
    epic: str
    direction: str
    size: str
    deal_id: str
    strategy_id: str
    entry_timestamp: str
    entry_price: str
    bid: str
    ask: str
    executable_mark: str
    stop: str
    target: str
    initial_risk: str
    current_risk: str
    unrealized_pnl: str
    pnl_currency: str
    current_r: str
    duration: str
    lifecycle: str


@dataclass(frozen=True)
class PerformanceState:
    available: bool
    source_label: str
    message: str
    metrics: tuple[tuple[str, str], ...] = ()
    breakdowns: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RiskState:
    portfolio_risk: str
    daily_pnl: str
    daily_loss_limit: str
    reconciliation_status: OperatorStatus
    open_positions: int
    working_orders: str
    critical_error: str


@dataclass(frozen=True)
class SystemHealthState:
    rest_health: OperatorStatus
    streaming_health: OperatorStatus
    price_freshness: OperatorStatus
    worker_health: OperatorStatus
    execution_authority: OperatorStatus
    approved_epic_count: int
    approved_strategy_count: int
    source_label: str


@dataclass(frozen=True)
class ResearchStatus:
    research_id: str
    status: str
    tested: str
    qualified: str
    message: str
    source_label: str


@dataclass(frozen=True)
class ControlCenterState:
    """All state a page needs, prepared by read-only source adapters."""

    source_label: str
    simulated: bool
    robot: RobotState
    broker: BrokerState
    instruments: tuple[InstrumentState, ...]
    strategies: tuple[StrategyState, ...]
    decisions: tuple[DecisionState, ...]
    positions: tuple[PositionState, ...]
    unclassified_broker_position_count: int
    performance: PerformanceState
    risk: RiskState
    health: SystemHealthState
    research: tuple[ResearchStatus, ...]
    start_gate: StartGate
