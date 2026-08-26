"""Local-only Demo Operator control boundary."""

from __future__ import annotations

import streamlit as st

from dashboard.models import ControlCenterState
from dashboard.operator_control import controls_enabled, invoke_local_controller
from dashboard.sources.demo_operator import DemoOperatorSnapshot


def render(snapshot: DemoOperatorSnapshot, state: ControlCenterState) -> None:
    st.header("Demo Operator")
    st.caption(
        "Local IG Demo control boundary. This page never creates a broker client or calls an IG "
        "order "
        "endpoint; requests go only to the separately guarded DQ-02 controller."
    )
    _status_strip(state)
    if state.simulated:
        st.warning("SIMULATED UI DATA — Demo controls are unavailable in mock/replay mode.")
        return
    if not controls_enabled():
        st.warning(
            "READ ONLY: controls require DEMO_OPERATOR_LOCAL=true on a local, non-hosted dashboard."
        )
    else:
        _controls(state)
    _alerts(snapshot)


def _status_strip(state: ControlCenterState) -> None:
    with st.container(horizontal=True):
        st.metric("Environment", state.robot.environment, border=True)
        st.metric("REST", state.broker.rest_status, border=True)
        st.metric("Streaming", state.broker.streaming_status, border=True)
        st.metric("Robot", state.robot.state, border=True)
        st.metric("Account", state.broker.account_status, border=True)
        st.metric("Kill switch", state.robot.kill_switch, border=True)
    with st.container(horizontal=True):
        st.metric("Approved EPICs", str(state.health.approved_epic_count), border=True)
        st.metric("Approved strategies", str(state.health.approved_strategy_count), border=True)
        st.metric("Execution authority", state.robot.execution_authority, border=True)
        st.metric("Reconciliation", state.risk.reconciliation_status, border=True)
    st.caption(f"Last successful IG synchronization: {state.broker.last_successful_read}")


def _controls(state: ControlCenterState) -> None:
    st.subheader("Local Demo controls")
    st.info(
        "Start is enabled only after every authority gate passes. "
        "Pause keeps reconciliation active; "
        "Stop safely ends the worker; Emergency Kill independently blocks execution."
    )
    if not state.start_gate.enabled:
        st.error("START DEMO ROBOT DISABLED")
        for blocker in state.start_gate.blockers:
            st.write(f"- {blocker}")
    start, pause, resume, stop, kill = st.columns(5)
    if start.button(
        "START DEMO ROBOT",
        type="primary",
        disabled=not state.start_gate.enabled,
        key="dq02_start",
    ):
        st.info(invoke_local_controller("start"))
    if pause.button("PAUSE NEW ENTRIES", disabled=state.robot.state != "RUNNING", key="dq02_pause"):
        st.info(invoke_local_controller("pause"))
    if resume.button("RESUME", disabled=state.robot.state != "PAUSED", key="dq02_resume"):
        st.info(invoke_local_controller("resume"))
    if stop.button(
        "STOP", disabled=state.robot.state not in {"RUNNING", "PAUSED"}, key="dq02_stop"
    ):
        st.info(invoke_local_controller("stop"))
    if kill.button("EMERGENCY KILL", type="primary", key="dq02_kill"):
        st.warning(invoke_local_controller("kill"))
    st.divider()
    confirmed = st.checkbox(
        "I understand this will close every locally owned, reconciled IG Demo position.",
        key="dq02_flatten_confirm",
    )
    if st.button("FLATTEN ROBOT POSITIONS", disabled=not confirmed, key="dq02_flatten"):
        st.warning(invoke_local_controller("flatten"))
    st.caption(
        "Flatten is a separate, explicitly confirmed action. "
        "Emergency Kill never flattens positions; "
        "Resume never releases an Emergency Kill."
    )


def _alerts(snapshot: DemoOperatorSnapshot) -> None:
    st.subheader("Operator alerts")
    fields = snapshot.fields
    message = fields.get("message")
    if isinstance(message, str):
        st.info(message)
    alerts = fields.get("alerts")
    if isinstance(alerts, list):
        for item in alerts:
            if isinstance(item, str):
                st.warning(item)
