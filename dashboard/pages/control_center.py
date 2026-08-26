"""The eight operator-facing Control Center MVP page renderers."""

from __future__ import annotations

import streamlit as st

from dashboard.components import render_summary_cards
from dashboard.models import ControlCenterState, DecisionState, PositionState


def render_simulation_banner(state: ControlCenterState) -> None:
    """Make explicit replay data impossible to mistake for broker state."""

    if state.simulated:
        st.warning("SIMULATED UI DATA — Mock/replay mode. No Demo or Live state is being shown.")


def render_cockpit(state: ControlCenterState) -> None:
    st.header("Cockpit")
    if state.robot.environment == "IG LIVE":
        st.error("LIVE EXECUTION NOT AUTHORIZED")
    else:
        st.info("IG DEMO MODE — local operator view; opening this dashboard never starts trading.")
    if state.health.approved_strategy_count == 0:
        st.error(
            "ROBOT CANNOT START NEW TRADES\n\n"
            "Reason: No strategies are currently approved for Demo execution."
        )
    render_summary_cards(
        (
            ("Environment", state.robot.environment, "Demo and Live are deliberately distinct."),
            (
                "Robot",
                state.robot.state,
                "Local worker state from the sanitized operator snapshot.",
            ),
            ("IG REST", state.broker.rest_status, "Read-only connection state."),
            (
                "IG streaming",
                state.broker.streaming_status,
                "Stale or unknown data blocks entries.",
            ),
            (
                "Execution authority",
                state.robot.execution_authority,
                "No page grants execution authority.",
            ),
            ("Kill switch", state.robot.kill_switch, "Emergency Kill never flattens positions."),
            ("Markets monitored", str(len(state.instruments)), "Verified research universe only."),
            (
                "Historically qualified strategies",
                "0",
                "No historical qualification is claimed without evidence.",
            ),
            (
                "Demo-approved strategies",
                str(state.health.approved_strategy_count),
                state.health.source_label,
            ),
            (
                "Open robot positions",
                str(state.risk.open_positions),
                "Reconciled strategy-owned positions only.",
            ),
            (
                "Today P&L",
                state.risk.daily_pnl,
                "Native currency is retained when conversion is unproven.",
            ),
            ("Today R", _not_available(), "No qualified Demo performance source."),
            ("Current portfolio risk", state.risk.portfolio_risk, "Unknown risk remains visible."),
            ("Last robot decision", state.robot.last_decision, "No decision data is invented."),
        )
    )
    st.caption(f"Active source: {state.source_label}")


def render_market_scanner(state: ControlCenterState) -> None:
    st.header("Market Scanner")
    st.caption(
        "Verified research universe. Prices and EPICs remain UNKNOWN until a read-only source "
        "proves them."
    )
    asset_classes = ("All",) + tuple(sorted({item.asset_class for item in state.instruments}))
    research = ("All",) + tuple(sorted({item.research_status for item in state.instruments}))
    markets = ("All",) + tuple(sorted({item.market_status for item in state.instruments}))
    signals = ("All",) + tuple(sorted({item.signal for item in state.instruments}))
    asset, qualification, market, signal = st.columns(4)
    selected_asset = asset.selectbox("Asset class", asset_classes, key="cc_asset_class")
    selected_qualification = qualification.selectbox(
        "Qualification", research, key="cc_qualification"
    )
    selected_market = market.selectbox("Market status", markets, key="cc_market_status")
    selected_signal = signal.selectbox("Signal state", signals, key="cc_signal_state")
    rows = [
        _instrument_row(item)
        for item in state.instruments
        if (selected_asset == "All" or item.asset_class == selected_asset)
        and (selected_qualification == "All" or item.research_status == selected_qualification)
        and (selected_market == "All" or item.market_status == selected_market)
        and (selected_signal == "All" or item.signal == selected_signal)
    ]
    st.dataframe(rows, hide_index=True, width="stretch")


def render_strategy_center(state: ControlCenterState) -> None:
    st.header("Strategy Center")
    st.error("DEMO APPROVED STRATEGY COUNT = 0")
    st.caption(
        "Research status is display-only. Only the integrated approved Demo execution registry can "
        "grant authority; it is currently empty."
    )
    st.subheader("Execution approval")
    st.dataframe(
        [_strategy_row(item) for item in state.strategies], hide_index=True, width="stretch"
    )
    st.subheader("External research status — read-only")
    st.dataframe(
        [
            {
                "Research": item.research_id,
                "Status": item.status,
                "Tested": item.tested,
                "Qualified": item.qualified,
                "Message": item.message,
                "Source": item.source_label,
            }
            for item in state.research
        ],
        hide_index=True,
        width="stretch",
    )


def render_positions(state: ControlCenterState) -> None:
    st.header("Positions")
    st.caption(
        "Only robot-owned, reconciled positions appear here. BUY marks at BID; SELL marks at OFFER."
    )
    if state.unclassified_broker_position_count:
        st.error(
            f"{state.unclassified_broker_position_count} broker position(s) have unknown strategy "
            "ownership; "
            "automatic trading remains blocked."
        )
    if not state.positions:
        st.info("No robot-owned positions.")
        return
    st.dataframe(
        [_position_row(item) for item in state.positions], hide_index=True, width="stretch"
    )


def render_performance(state: ControlCenterState) -> None:
    st.header("Performance")
    period = st.selectbox(
        "Period",
        ("TODAY", "THIS WEEK", "THIS MONTH", "LAST 30 DAYS", "SINCE DEMO START", "CUSTOM RANGE"),
        key="cc_performance_period",
    )
    if period == "CUSTOM RANGE":
        start, end = st.columns(2)
        start.date_input("Start date", key="cc_custom_start")
        end.date_input("End date", key="cc_custom_end")
    if not state.performance.available:
        st.info(state.performance.message)
        st.info("DEMO EVIDENCE NOT AVAILABLE YET")
    else:
        render_summary_cards(
            tuple(
                (label, value, state.performance.source_label)
                for label, value in state.performance.metrics
            )
        )
        st.bar_chart(
            {
                label: [float(value.split()[-1].removesuffix("R"))]
                for label, value in state.performance.breakdowns
            }
        )
    st.subheader("Historical versus Demo")
    st.info("DEMO EVIDENCE NOT AVAILABLE YET")
    st.caption("Historical, Paper, Demo, and Live performance are never mixed into one result.")


def render_decision_explorer(state: ControlCenterState) -> None:
    st.header("Decision Explorer")
    st.caption("Operator-friendly decision history. Technical logs remain separate from this view.")
    if not state.decisions:
        st.info("No robot decision source is available. No decision data is fabricated.")
        return
    st.dataframe(
        [
            {
                "Timestamp": item.timestamp,
                "Instrument": item.instrument,
                "Decision": item.outcome,
                "Reason": item.primary_reason,
            }
            for item in state.decisions
        ],
        hide_index=True,
        width="stretch",
    )
    for decision in state.decisions:
        with st.expander(f"{decision.timestamp} — {decision.instrument} — {decision.outcome}"):
            st.write(f"**Primary reason:** {decision.primary_reason}")
            st.dataframe([_decision_chain_row(decision)], hide_index=True, width="stretch")


def render_risk_health(state: ControlCenterState) -> None:
    st.header("Risk & Health")
    render_summary_cards(
        (
            ("IG REST health", state.health.rest_health, state.broker.source_label),
            ("IG streaming health", state.health.streaming_health, state.broker.source_label),
            (
                "Price freshness",
                state.health.price_freshness,
                "Unknown or stale data blocks entries.",
            ),
            ("Worker / singleton", state.health.worker_health, "Durable local worker state."),
            (
                "Execution authority",
                state.robot.execution_authority,
                "Explicitly OFF in current state.",
            ),
            (
                "Approved EPIC registry",
                str(state.health.approved_epic_count),
                state.health.source_label,
            ),
            (
                "Approved strategy registry",
                str(state.health.approved_strategy_count),
                state.health.source_label,
            ),
            ("Open robot positions", str(state.risk.open_positions), "Reconciled ownership only."),
            ("Working orders", state.risk.working_orders, "No value is assumed."),
            ("Portfolio risk", state.risk.portfolio_risk, "No risk calculation happens in the UI."),
            ("Daily P&L", state.risk.daily_pnl, "Native currency when conversion is unproven."),
            (
                "Daily loss limit",
                state.risk.daily_loss_limit,
                "Invalid or unknown state blocks start.",
            ),
            (
                "Reconciliation",
                state.risk.reconciliation_status,
                "Unknown ownership is fail-closed.",
            ),
            ("Last critical error", state.risk.critical_error, "Sanitized operator source only."),
        )
    )
    with st.expander("Diagnostics"):
        st.write(f"**Source:** {state.source_label}")
        st.write(f"**Last successful auth/read:** {state.broker.last_successful_read}")
        st.write(
            "This dashboard does not expose credentials, raw tokens, or technical stack traces."
        )


def _instrument_row(item: object) -> dict[str, str]:
    return {
        "Instrument": item.instrument,
        "EPIC": item.epic,
        "Asset class": item.asset_class,
        "Market": item.market_status,
        "Bid": item.bid,
        "Ask": item.ask,
        "Spread": item.spread,
        "Data freshness": item.data_freshness,
        "Streaming": item.streaming,
        "Research": item.research_status,
        "Approved strategy": item.approved_strategy,
        "Strategy status": item.strategy_status,
        "Signal": item.signal,
        "Block reason": item.block_reason,
    }


def _strategy_row(item: object) -> dict[str, str]:
    return {
        "Strategy": item.strategy_id,
        "Family": item.family,
        "Instrument": item.instrument,
        "Timeframe": item.timeframe,
        "Historical": item.historical_status,
        "Demo approval": item.demo_approval,
        "Authority": item.execution_authority,
        "Trades": item.trade_count,
        "OOS expectancy": item.oos_expectancy,
        "Walk-forward expectancy": item.walk_forward_expectancy,
        "Profit factor": item.profit_factor,
        "Max drawdown": item.max_drawdown,
        "Stress": item.stress_status,
        "Reason": item.reason,
        "Source": item.source_label,
    }


def _position_row(item: PositionState) -> dict[str, str]:
    return {
        "Instrument": item.instrument,
        "EPIC": item.epic,
        "Direction": item.direction,
        "Size": item.size,
        "Deal ID": item.deal_id,
        "Strategy": item.strategy_id,
        "Entry timestamp": item.entry_timestamp,
        "Entry": item.entry_price,
        "Bid": item.bid,
        "Ask": item.ask,
        "Executable mark": item.executable_mark,
        "Stop": item.stop,
        "Target": item.target,
        "Initial risk": item.initial_risk,
        "Current risk": item.current_risk,
        "Unrealized P&L": item.unrealized_pnl,
        "P&L currency": item.pnl_currency,
        "Current R": item.current_r,
        "Duration": item.duration,
        "Lifecycle": item.lifecycle,
    }


def _decision_chain_row(item: DecisionState) -> dict[str, str]:
    return {
        "Market tradeable": item.market_tradeable,
        "Data fresh": item.data_fresh,
        "Strategy qualified": item.strategy_qualified,
        "Strategy approved": item.strategy_approved,
        "Signal detected": item.signal_detected,
        "Opportunity/cost": item.opportunity_acceptable,
        "Spread acceptable": item.spread_acceptable,
        "Risk available": item.risk_available,
        "Portfolio exposure": item.portfolio_exposure,
        "Kill switch released": item.kill_switch_released,
        "Execution authority": item.execution_authority,
    }


def _not_available() -> str:
    return "NOT AVAILABLE"
