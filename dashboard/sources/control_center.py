"""Prepared, read-only state for the operator-facing Control Center.

The dashboard consumes reviewed project gates, a sanitized local operator
snapshot, optional local research summaries, and an explicit mock mode.  It
does not import trading code, open the Demo SQLite store, or create a broker
client.  In particular, external research is display-only and cannot become
an execution approval through this adapter.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from dashboard.models import (
    BrokerState,
    ControlCenterState,
    DecisionState,
    InstrumentState,
    OperatorStatus,
    PerformanceState,
    PositionState,
    ProjectStatus,
    ResearchStatus,
    RiskState,
    RobotState,
    StartGate,
    StrategyState,
    SystemHealthState,
)
from dashboard.operator_control import controls_enabled
from dashboard.sources.demo_operator import (
    DemoOperatorSnapshot,
    research_instrument_rows,
    strategy_catalog,
)

_VERIFIED_RESEARCH_UNIVERSE = (
    "EURUSD",
    "GBPUSD",
    "EURGBP",
    "USDJPY",
    "EURJPY",
    "GBPJPY",
    "AUDUSD",
    "NZDUSD",
    "USDCAD",
    "USDCHF",
    "EURCHF",
    "EURAUD",
    "GBPAUD",
    "AUDJPY",
    "CADJPY",
    "CHFJPY",
    "XAUUSD",
    "XAGUSD",
    "US500",
    "USTECH100",
)
_EXPERIMENTAL_RESEARCH_IDS = ("SL-05", "Phase E", "MICRO-01", "ALPHA-02", "ALPHA-03")
_UNKNOWN = "UNKNOWN"
_NOT_AVAILABLE = "NOT AVAILABLE"


def load_control_center_state(
    project: ProjectStatus,
    operator_snapshot: DemoOperatorSnapshot,
) -> ControlCenterState:
    """Build a single page-ready state from only read-only sources.

    ``CONTROL_CENTER_MODE=MOCK`` is intentionally explicit.  It never mixes
    simulated rows with the local Demo source and it always returns a disabled
    start gate.
    """

    if os.environ.get("CONTROL_CENTER_MODE", "").upper() == "MOCK":
        return _mock_state()

    fields = operator_snapshot.fields if operator_snapshot.available else {}
    environment = _environment(fields.get("environment"))
    rest = _label(fields.get("rest_status"), _UNKNOWN)
    streaming = _label(fields.get("streaming_status"), _UNKNOWN)
    robot_status = _label(fields.get("robot_state"), "STOPPED")
    kill_switch = _label(fields.get("kill_switch_state"), _UNKNOWN)
    account = _label(fields.get("account"), "NOT VALIDATED")
    execution_authority = _label(fields.get("execution_authority"), "OFF")
    approved_epic_count = _count(fields.get("approved_demo_epic_count"))
    approved_strategy_count = _count(fields.get("approved_demo_strategy_count"))

    broker = BrokerState(
        rest_status=rest,
        streaming_status=streaming,
        account_status=account,
        last_successful_read=_label(fields.get("last_successful_sync"), _NOT_AVAILABLE),
        source_label=(
            "LOCAL DEMO OPERATOR SNAPSHOT (READ-ONLY)"
            if operator_snapshot.available
            else "NO LOCAL OPERATOR SNAPSHOT"
        ),
    )
    positions, unclassified = _positions(fields.get("positions"))
    reconciliation = _operator_status(fields.get("reconciliation_status"), "UNKNOWN")
    if reconciliation == "NORMAL" and unclassified:
        reconciliation = "BLOCKED"
    if not operator_snapshot.available:
        reconciliation = "UNKNOWN"
    health = SystemHealthState(
        rest_health=_health_from_connection(rest),
        streaming_health=_health_from_streaming(streaming),
        price_freshness="UNKNOWN" if streaming != "CONNECTED" else "NORMAL",
        worker_health=_health_from_worker(robot_status),
        execution_authority="NORMAL" if execution_authority == "ON" else "BLOCKED",
        approved_epic_count=approved_epic_count,
        approved_strategy_count=approved_strategy_count,
        source_label="APPROVED DEMO EXECUTION REGISTRY: EMPTY",
    )
    risk = RiskState(
        portfolio_risk=_NOT_AVAILABLE,
        daily_pnl=_label(fields.get("today_realized_pnl"), _NOT_AVAILABLE),
        daily_loss_limit=_label(fields.get("risk_configuration_status"), "UNKNOWN"),
        reconciliation_status=reconciliation,
        open_positions=len(positions),
        working_orders=_label(fields.get("working_orders"), _NOT_AVAILABLE),
        critical_error=_label(fields.get("last_critical_error"), _last_critical_error(fields)),
    )
    robot = RobotState(
        environment=environment,
        state=robot_status,
        execution_authority=execution_authority,
        kill_switch=kill_switch,
        singleton_status=_health_from_worker(robot_status),
        last_decision=_NOT_AVAILABLE,
    )
    research = load_research_statuses()
    state = ControlCenterState(
        source_label="PROJECT GATES + LOCAL READ-ONLY OPERATOR SNAPSHOT",
        simulated=False,
        robot=robot,
        broker=broker,
        instruments=_instrument_states(),
        strategies=_strategy_states(research),
        decisions=(),
        positions=positions,
        unclassified_broker_position_count=unclassified,
        performance=PerformanceState(
            available=False,
            source_label="DEMO PERFORMANCE STORE: NOT AVAILABLE",
            message="DEMO EVIDENCE NOT AVAILABLE YET",
        ),
        risk=risk,
        health=health,
        research=research,
        start_gate=StartGate(False, ()),
    )
    return ControlCenterState(
        **{
            **state.__dict__,
            "start_gate": _start_gate(project, state, operator_snapshot.available),
        }
    )


def load_research_statuses() -> tuple[ResearchStatus, ...]:
    """Load configured external JSON summaries as read-only research evidence.

    The adapter ignores every execution-related value in a research file.
    Research IDs can improve the research display only; they cannot add a
    strategy, EPIC, authority flag, or start-gate permission.
    """

    defaults = {
        item: ResearchStatus(
            research_id=item,
            status="NOT AVAILABLE",
            tested=_NOT_AVAILABLE,
            qualified=_NOT_AVAILABLE,
            message="No configured read-only research summary is available.",
            source_label="NO EXTERNAL RESEARCH SOURCE",
        )
        for item in _EXPERIMENTAL_RESEARCH_IDS
    }
    raw_paths = os.environ.get("CONTROL_CENTER_RESEARCH_SUMMARIES", "")
    for value in (part for part in raw_paths.split(os.pathsep) if part.strip()):
        path = Path(value)
        for item in _read_research_document(path):
            defaults[item.research_id] = item
    return tuple(defaults[key] for key in _EXPERIMENTAL_RESEARCH_IDS)


def _read_research_document(path: Path) -> tuple[ResearchStatus, ...]:
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ()
    items: Iterable[object]
    if isinstance(document, Mapping):
        listed = document.get("research")
        items = listed if isinstance(listed, list) else (document,)
    elif isinstance(document, list):
        items = document
    else:
        return ()
    result: list[ResearchStatus] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        research_id = _label(raw.get("research_id", raw.get("id")), "")
        if research_id not in _EXPERIMENTAL_RESEARCH_IDS:
            continue
        result.append(
            ResearchStatus(
                research_id=research_id,
                status=_label(raw.get("status"), _UNKNOWN),
                tested=_label(raw.get("tested", raw.get("strategies_tested")), _NOT_AVAILABLE),
                qualified=_label(raw.get("qualified", raw.get("qualified_count")), _NOT_AVAILABLE),
                message=_label(raw.get("message", raw.get("reason")), _NOT_AVAILABLE),
                source_label=f"EXTERNAL READ-ONLY RESEARCH: {path.name}",
            )
        )
    return tuple(result)


def _instrument_states() -> tuple[InstrumentState, ...]:
    rows = {str(row["Instrument"]): row for row in research_instrument_rows()}
    result: list[InstrumentState] = []
    for symbol in _VERIFIED_RESEARCH_UNIVERSE:
        row = rows.get(symbol, {})
        epic = _unknown_if_unavailable(row.get("IG EPIC"))
        result.append(
            InstrumentState(
                instrument=symbol,
                epic=epic,
                asset_class=_label(row.get("Asset class"), _UNKNOWN),
                market_status=_unknown_if_unavailable(row.get("Market")),
                bid=_NOT_AVAILABLE,
                ask=_NOT_AVAILABLE,
                spread=_unknown_if_unavailable(row.get("Spread")),
                data_freshness=_unknown_if_unavailable(row.get("Data status")),
                streaming=_unknown_if_unavailable(row.get("Streaming")),
                research_status=_label(row.get("Qualification"), "RESEARCH"),
                approved_strategy="NONE",
                strategy_status=_label(row.get("Strategy status"), "RESEARCH"),
                signal=_NOT_AVAILABLE,
                block_reason="No strategy has a Demo execution approval.",
            )
        )
    return tuple(result)


def _strategy_states(research: tuple[ResearchStatus, ...]) -> tuple[StrategyState, ...]:
    catalog = strategy_catalog()
    rows: list[StrategyState] = []
    for strategy_id in sorted(catalog):
        item = catalog[strategy_id]
        rows.append(
            StrategyState(
                strategy_id=strategy_id,
                family=strategy_id,
                instrument="RESEARCH UNIVERSE",
                timeframe=_label(item.get("preferred_timeframe"), "RESEARCH-DEFINED"),
                historical_status="NOT QUALIFIED",
                demo_approval="NOT APPROVED",
                execution_authority="OFF",
                trade_count=_NOT_AVAILABLE,
                oos_expectancy=_NOT_AVAILABLE,
                walk_forward_expectancy=_NOT_AVAILABLE,
                profit_factor=_NOT_AVAILABLE,
                max_drawdown=_NOT_AVAILABLE,
                stress_status=_NOT_AVAILABLE,
                reason="No historically qualified strategy/instrument combination is available.",
                source_label="APPROVED DEMO EXECUTION REGISTRY: EMPTY",
            )
        )
    rows.extend(
        StrategyState(
            strategy_id=item.research_id,
            family="EXTERNAL RESEARCH",
            instrument="NOT AVAILABLE",
            timeframe="NOT AVAILABLE",
            historical_status=item.status,
            demo_approval="NOT APPROVED",
            execution_authority="OFF",
            trade_count=item.tested,
            oos_expectancy=_NOT_AVAILABLE,
            walk_forward_expectancy=_NOT_AVAILABLE,
            profit_factor=_NOT_AVAILABLE,
            max_drawdown=_NOT_AVAILABLE,
            stress_status=_NOT_AVAILABLE,
            reason=item.message,
            source_label=item.source_label,
        )
        for item in research
    )
    return tuple(rows)


def _positions(raw: object) -> tuple[tuple[PositionState, ...], int]:
    if not isinstance(raw, list):
        return (), 0
    positions: list[PositionState] = []
    unclassified = 0
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        if item.get("ownership") != "RECONCILED":
            unclassified += 1
            continue
        direction = _label(item.get("direction"), _UNKNOWN).upper()
        bid = _label(item.get("bid"), _NOT_AVAILABLE)
        ask = _label(item.get("offer", item.get("ask")), _NOT_AVAILABLE)
        mark = bid if direction == "BUY" else ask if direction == "SELL" else _NOT_AVAILABLE
        positions.append(
            PositionState(
                instrument=_label(item.get("instrument"), _UNKNOWN),
                epic=_label(item.get("epic"), _UNKNOWN),
                direction=direction,
                size=_label(item.get("size"), _NOT_AVAILABLE),
                deal_id=_redacted_deal_id(item.get("deal_id")),
                strategy_id=_label(item.get("strategy_id"), _NOT_AVAILABLE),
                entry_timestamp=_label(item.get("entry_timestamp"), _NOT_AVAILABLE),
                entry_price=_label(item.get("entry"), _NOT_AVAILABLE),
                bid=bid,
                ask=ask,
                executable_mark=mark,
                stop=_label(item.get("stop"), _NOT_AVAILABLE),
                target=_label(item.get("target"), _NOT_AVAILABLE),
                initial_risk=_label(item.get("initial_risk"), _NOT_AVAILABLE),
                current_risk=_label(item.get("current_risk"), _NOT_AVAILABLE),
                unrealized_pnl=_label(item.get("unrealized_pnl"), _NOT_AVAILABLE),
                pnl_currency=_label(item.get("currency"), _NOT_AVAILABLE),
                current_r=_label(item.get("current_r"), _NOT_AVAILABLE),
                duration=_label(item.get("duration"), _NOT_AVAILABLE),
                lifecycle="RECONCILED",
            )
        )
    return tuple(positions), unclassified


def _start_gate(
    project: ProjectStatus,
    state: ControlCenterState,
    snapshot_available: bool,
) -> StartGate:
    blockers: list[str] = []
    if state.robot.environment != "IG DEMO":
        blockers.append("The configured environment is not IG Demo.")
    if not controls_enabled():
        blockers.append("DEMO_OPERATOR_LOCAL=true is required on a local, non-hosted dashboard.")
    if not snapshot_available or state.broker.rest_status != "CONNECTED":
        blockers.append("The IG Demo account has not been validated by a local read-only snapshot.")
    if state.robot.kill_switch != "RELEASED":
        blockers.append("Kill switch is active or not proven released.")
    if state.robot.singleton_status != "NORMAL":
        blockers.append("Execution lease or singleton health is not proven normal.")
    if state.robot.execution_authority != "ON":
        blockers.append("Execution authority is OFF.")
    if state.health.approved_epic_count == 0:
        blockers.append("Approved Demo EPIC registry is empty.")
    if state.health.approved_strategy_count == 0:
        blockers.append("No strategies are currently approved for Demo execution.")
    if state.risk.daily_loss_limit == "UNKNOWN":
        blockers.append("Risk configuration is not validated by the operator source.")
    if state.risk.reconciliation_status != "NORMAL":
        blockers.append("Reconciliation state is not proven safe.")
    if project.summary.demo_execution != "PASS":
        blockers.append("Reviewed Demo execution governance is not authorized.")
    unique_blockers = tuple(dict.fromkeys(blockers))
    return StartGate(enabled=not unique_blockers, blockers=unique_blockers)


def _mock_state() -> ControlCenterState:
    """Return only simulated data, never a blended broker/Mock response."""

    position = PositionState(
        instrument="EURGBP",
        epic="CS.D.EURGBP.MINI.IP",
        direction="BUY",
        size="1.00",
        deal_id="SIM…0001",
        strategy_id="SIM-S3",
        entry_timestamp="2026-08-26T09:30:00Z",
        entry_price="0.85000",
        bid="0.85020",
        ask="0.85023",
        executable_mark="0.85020",
        stop="0.84920",
        target="0.85160",
        initial_risk="SIMULATED 1.0R",
        current_risk="SIMULATED 0.8R",
        unrealized_pnl="SIMULATED 20.00",
        pnl_currency="GBP",
        current_r="SIMULATED +0.2R",
        duration="SIMULATED 00:30:00",
        lifecycle="SIMULATED RECONCILED",
    )
    instrument = InstrumentState(
        instrument="EURGBP",
        epic="CS.D.EURGBP.MINI.IP",
        asset_class="FX",
        market_status="SIMULATED TRADEABLE",
        bid="0.85020",
        ask="0.85023",
        spread="0.00003",
        data_freshness="SIMULATED FRESH",
        streaming="SIMULATED CONNECTED",
        research_status="SIMULATED RESEARCH",
        approved_strategy="NONE",
        strategy_status="SIMULATED WATCHING",
        signal="SIMULATED WATCHING",
        block_reason="Simulation only; no execution authority exists.",
    )
    decision = DecisionState(
        timestamp="2026-08-26T10:00:00Z",
        instrument="EURGBP",
        outcome="NO TRADE",
        primary_reason="SIMULATED: No approved strategy.",
        market_tradeable="PASS",
        data_fresh="PASS",
        strategy_qualified="FAIL",
        strategy_approved="FAIL",
        signal_detected="WATCHING",
        opportunity_acceptable="NOT EVALUATED",
        spread_acceptable="PASS",
        risk_available="PASS",
        portfolio_exposure="PASS",
        kill_switch_released="PASS",
        execution_authority="FAIL",
    )
    research = tuple(
        ResearchStatus(
            research_id=item,
            status="SIMULATED",
            tested="SIMULATED",
            qualified="SIMULATED",
            message="SIMULATED UI DATA",
            source_label="MOCK / REPLAY SOURCE",
        )
        for item in _EXPERIMENTAL_RESEARCH_IDS
    )
    return ControlCenterState(
        source_label="MOCK / REPLAY SOURCE",
        simulated=True,
        robot=RobotState(
            environment="SIMULATED UI DATA",
            state="SIMULATED",
            execution_authority="OFF",
            kill_switch="SIMULATED",
            singleton_status="UNKNOWN",
            last_decision="SIMULATED NO TRADE",
        ),
        broker=BrokerState(
            rest_status="SIMULATED CONNECTED",
            streaming_status="SIMULATED CONNECTED",
            account_status="SIMULATED",
            last_successful_read="SIMULATED",
            source_label="MOCK / REPLAY SOURCE",
        ),
        instruments=(instrument,),
        strategies=_strategy_states(research),
        decisions=(decision,),
        positions=(position,),
        unclassified_broker_position_count=0,
        performance=PerformanceState(
            available=True,
            source_label="MOCK / REPLAY SOURCE",
            message="SIMULATED UI DATA",
            metrics=(("Net P&L", "SIMULATED +20.00 GBP"), ("Net R", "SIMULATED +0.2R")),
            breakdowns=(("EURGBP", "SIMULATED +0.2R"),),
        ),
        risk=RiskState(
            portfolio_risk="SIMULATED 0.8R",
            daily_pnl="SIMULATED +20.00 GBP",
            daily_loss_limit="SIMULATED NORMAL",
            reconciliation_status="UNKNOWN",
            open_positions=1,
            working_orders="SIMULATED 0",
            critical_error="SIMULATED UI DATA",
        ),
        health=SystemHealthState(
            rest_health="NORMAL",
            streaming_health="NORMAL",
            price_freshness="NORMAL",
            worker_health="UNKNOWN",
            execution_authority="BLOCKED",
            approved_epic_count=0,
            approved_strategy_count=0,
            source_label="MOCK / REPLAY SOURCE",
        ),
        research=research,
        start_gate=StartGate(False, ("SIMULATED UI DATA cannot start the Demo robot.",)),
    )


def _environment(value: object) -> str:
    label = _label(value, "IG DEMO").upper().replace("_", " ")
    if "LIVE" in label:
        return "IG LIVE"
    return "IG DEMO"


def _health_from_connection(value: str) -> OperatorStatus:
    if value == "CONNECTED":
        return "NORMAL"
    if value == "DISCONNECTED":
        return "BLOCKED"
    return "UNKNOWN"


def _health_from_streaming(value: str) -> OperatorStatus:
    if value == "CONNECTED":
        return "NORMAL"
    if value in {"STALE", "DISCONNECTED"}:
        return "WARNING"
    return "UNKNOWN"


def _health_from_worker(value: str) -> OperatorStatus:
    if value in {"STOPPED", "RUNNING", "PAUSED"}:
        return "NORMAL"
    if value in {"SAFE_STOP", "STOP_REQUESTED"}:
        return "BLOCKED"
    return "UNKNOWN"


def _operator_status(value: object, fallback: OperatorStatus) -> OperatorStatus:
    label = _label(value, fallback).upper()
    return label if label in {"NORMAL", "WARNING", "BLOCKED", "UNKNOWN"} else fallback


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _redacted_deal_id(value: object) -> str:
    deal_id = _label(value, _NOT_AVAILABLE)
    return deal_id if len(deal_id) <= 8 else f"{deal_id[:4]}…{deal_id[-4:]}"


def _last_critical_error(fields: Mapping[str, object]) -> str:
    alerts = fields.get("alerts")
    if isinstance(alerts, list) and alerts:
        first = alerts[0]
        if isinstance(first, str):
            return first
    return _NOT_AVAILABLE


def _unknown_if_unavailable(value: object) -> str:
    label = _label(value, _UNKNOWN)
    unavailable = {"not discovered", "not synchronized", "unavailable"}
    return _UNKNOWN if label.casefold() in unavailable else label


def _label(value: object, fallback: str) -> str:
    if value is None:
        return fallback
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else fallback
    if isinstance(value, int | float):
        return str(value)
    return fallback
