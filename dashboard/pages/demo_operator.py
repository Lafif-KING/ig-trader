"""Local-only operator page; existing Control Center pages remain read-only."""

# ruff: noqa: E501

from __future__ import annotations

import streamlit as st

from dashboard.operator_control import controls_enabled, invoke_local_controller
from dashboard.sources.demo_operator import (
    DemoOperatorSnapshot,
    research_instrument_rows,
    resolution_detail,
    strategy_catalog,
)


def render(snapshot: DemoOperatorSnapshot) -> None:
    st.header("Demo Operator")
    st.caption(
        "Local IG Demo qualification console. Live execution has no control on this page and remains "
        "impossible. Broker facts are supplied only by the separate local worker."
    )
    _status_strip(snapshot)
    enabled = controls_enabled()
    if not enabled:
        st.warning(
            "READ ONLY: controls require DEMO_OPERATOR_LOCAL=true on a local, non-hosted dashboard."
        )
    else:
        _controls()
    _alerts(snapshot)
    _positions(snapshot)
    _instrument_table_and_detail()


def _status_strip(snapshot: DemoOperatorSnapshot) -> None:
    fields = snapshot.fields
    with st.container(horizontal=True):
        st.metric("Environment", str(fields.get("environment", "IG_DEMO")), border=True)
        st.metric("REST", str(fields.get("rest_status", "DISCONNECTED")), border=True)
        st.metric("Streaming", str(fields.get("streaming_status", "DISCONNECTED")), border=True)
        st.metric("Robot", str(fields.get("robot_state", "STOPPED")), border=True)
        st.metric("Account", str(fields.get("account", "Not verified")), border=True)
        st.metric("Kill switch", str(fields.get("kill_switch_state", "BLOCKING")), border=True)
    with st.container(horizontal=True):
        st.metric("Balance", str(fields.get("balance", "Unavailable")), border=True)
        st.metric("Available funds", str(fields.get("available_funds", "Unavailable")), border=True)
        st.metric("Open positions", str(fields.get("total_open_positions", 0)), border=True)
        st.metric("Open P&L", str(fields.get("total_open_pnl", "Unavailable")), border=True)
        st.metric(
            "Today's Demo P&L", str(fields.get("today_realized_pnl", "Unavailable")), border=True
        )
    st.caption(
        f"Last successful IG synchronization: {fields.get('last_successful_sync', 'Not yet synchronized')}"
    )


def _controls() -> None:
    st.subheader("Local Demo controls")
    st.info(
        "Start launches one local worker. It must prove the Demo endpoint and expected account, reconcile "
        "broker positions, and pass its durable lock checks before it can run."
    )
    start, stop, kill = st.columns(3)
    if start.button("START DEMO ROBOT", type="primary", key="dq02_start"):
        st.info(invoke_local_controller("start"))
    if stop.button("STOP ROBOT", key="dq02_stop"):
        st.info(invoke_local_controller("stop"))
    if kill.button("EMERGENCY KILL", type="primary", key="dq02_kill"):
        st.warning(invoke_local_controller("kill"))
    st.divider()
    confirmed = st.checkbox(
        "I understand this will close every locally owned, reconciled IG Demo position.",
        key="dq02_flatten_confirm",
    )
    if st.button("CLOSE ALL DEMO POSITIONS", disabled=not confirmed, key="dq02_flatten"):
        st.warning(invoke_local_controller("flatten"))
    st.caption(
        "Flattening is separate from Emergency Kill. It first reads broker positions, refuses unknown "
        "ownership, then closes each exact reconciled deal through the DQ-01 confirmation path."
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


def _positions(snapshot: DemoOperatorSnapshot) -> None:
    st.subheader("Open IG Demo positions")
    positions = snapshot.fields.get("positions")
    if not isinstance(positions, list) or not positions:
        st.info("No synchronized IG Demo positions are available.")
        return
    st.dataframe(positions, hide_index=True, width="stretch")
    st.caption(
        "The broker position snapshot is authoritative. P&L must use the validated contract metadata, "
        "BUY bid or SELL offer mark, and stays native-currency when conversion cannot be proven."
    )


def _instrument_table_and_detail() -> None:
    rows = research_instrument_rows()
    st.subheader("Research and Demo registry")
    asset_classes = ("All",) + tuple(sorted({str(row["Asset class"]) for row in rows}))
    strategies = ("All",) + tuple(sorted({str(row["Assigned strategy"]) for row in rows}))
    qualifications = ("All",) + tuple(sorted({str(row["Qualification"]) for row in rows}))
    symbols = ("All",) + tuple(str(row["Instrument"]) for row in rows)
    position_statuses = ("All",) + tuple(sorted({str(row["Position"]) for row in rows}))
    first, second, third, fourth, fifth = st.columns(5)
    asset_filter = first.selectbox("Asset class", asset_classes, key="dq02_asset_filter")
    instrument_filter = second.selectbox("Instrument", symbols, key="dq02_instrument_filter")
    strategy_filter = third.selectbox("Strategy", strategies, key="dq02_strategy_filter")
    qualification_filter = fourth.selectbox(
        "Qualification", qualifications, key="dq02_qualification_filter"
    )
    position_filter = fifth.selectbox(
        "Position status", position_statuses, key="dq02_position_filter"
    )
    filtered = [
        row
        for row in rows
        if (asset_filter == "All" or row["Asset class"] == asset_filter)
        and (instrument_filter == "All" or row["Instrument"] == instrument_filter)
        and (strategy_filter == "All" or row["Assigned strategy"] == strategy_filter)
        and (qualification_filter == "All" or row["Qualification"] == qualification_filter)
        and (position_filter == "All" or row["Position"] == position_filter)
    ]
    st.dataframe(filtered, hide_index=True, width="stretch")
    selected_symbol = st.selectbox(
        "Instrument detail",
        [str(row["Instrument"]) for row in filtered] or ["No result"],
        key="dq02_detail",
    )
    selected = next((row for row in rows if row["Instrument"] == selected_symbol), None)
    if selected is None:
        return
    strategy_id = str(selected["Assigned strategy"])
    strategy = strategy_catalog()[strategy_id]
    st.subheader(f"{selected_symbol} strategy and qualification detail")
    st.write(f"**{strategy['name']} {strategy['version']}** — {strategy['description']}")
    st.write(f"**Why assigned:** {selected['Why assigned']}")
    st.write(f"**Family:** {strategy['family']}")
    st.write(f"**Market hypothesis:** {strategy['market_hypothesis']}")
    st.write(f"**Entry logic:** {strategy['entry']}")
    st.write(f"**Exit logic:** {strategy['exit']}")
    st.write(f"**Stop logic:** {strategy['stop_logic']}")
    st.write(f"**Target logic:** {strategy['target_logic']}")
    st.write(f"**Preferred session:** {strategy['preferred_session']}")
    st.write(f"**Preferred timeframe:** {strategy['preferred_timeframe']}")
    st.write(f"**Preferred regime:** {strategy['preferred_regime']}")
    st.write(f"**Known weaknesses:** {strategy['weaknesses']}")
    st.write(f"**Risk considerations:** {strategy['risk_considerations']}")
    st.write(f"**Why not trading:** {selected['Why not trading']}")
    evidence = resolution_detail(selected_symbol)
    if evidence and evidence.get("classification") == "AMBIGUOUS":
        candidates = evidence.get("candidates")
        if isinstance(candidates, list):
            st.write("**DQ-03 top candidates (selection blocked):**")
            st.dataframe(candidates, hide_index=True, width="stretch")
    st.write(
        "**Research results:** Local evidence is shown only when a verified artifact is available."
    )
    st.write(
        "**Demo results:** DEMO_NOT_STARTED until a separate permitted registration generates evidence."
    )
