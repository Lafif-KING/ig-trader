"""Read-only Streamlit presentation for the Shadow Tournament observer."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import streamlit as st

from dashboard.shadow_tournament_control import (
    ShadowTournamentControls,
    controls_for,
    invoke_shadow_tournament_controller,
)
from dashboard.sources.shadow_tournament import ShadowTournamentDashboard


def render(dashboard: ShadowTournamentDashboard) -> None:
    """Render Shadow evidence only; no broker or Demo component is constructed."""

    controls = controls_for(dashboard)
    st.header("Shadow Tournament")
    st.error("SHADOW ONLY—ZERO ORDERS")
    st.caption(
        "This local observer records no broker order, position, account, risk, or Demo-worker "
        "action. It is separate from the Demo Operator controls."
    )
    _render_identity(dashboard)
    _render_lifecycle(dashboard, controls)
    _render_market_matrix(dashboard.market_matrix)
    if not dashboard.available:
        st.warning("SHADOW TOURNAMENT DATA NOT AVAILABLE")
        st.write(f"**Reason:** {dashboard.reason}")
        return
    _render_provider_health(dashboard.provider_health)
    _render_epoch_readiness(dashboard.epoch_readiness)
    _render_market_snapshots(dashboard.market_snapshots)
    _render_engine_insights(dashboard.engine_insights)
    _render_decisions(dashboard.latest_decisions)
    _render_resolved_outcomes(dashboard.resolved_outcomes)
    _render_leaderboard(dashboard.leaderboard)
    _render_factor_audit(dashboard.factor_audit)


def _render_identity(dashboard: ShadowTournamentDashboard) -> None:
    version, fingerprint, authority = st.columns(3)
    version.metric("Tournament", dashboard.tournament_version)
    fingerprint.metric("Config fingerprint", dashboard.config_fingerprint)
    authority.metric("Execution authority", dashboard.execution_authority)


def _render_lifecycle(
    dashboard: ShadowTournamentDashboard,
    controls: ShadowTournamentControls,
) -> None:
    st.subheader("Observer lifecycle")
    st.write(f"**Epoch:** {dashboard.epoch_utc or 'NOT CREATED'}")
    st.write(f"**Monitor state:** {'RUNNING' if controls.monitor_running else 'NOT RUNNING'}")
    if not dashboard.epoch_created:
        st.warning("Start is disabled until a separately created Shadow Tournament epoch exists.")
    elif not controls.start_enabled and not controls.monitor_running:
        st.info(f"Monitor start remains blocked: {controls.reason}")
    start, stop = st.columns(2)
    if start.button(
        "START SHADOW MONITOR",
        disabled=not controls.start_enabled,
        key="shadow01_start",
    ):
        st.info(invoke_shadow_tournament_controller("start", dashboard))
    if stop.button(
        "STOP SHADOW MONITOR",
        disabled=not controls.stop_enabled,
        key="shadow01_stop",
    ):
        st.info(invoke_shadow_tournament_controller("stop", dashboard))
    st.caption("The controller can run only fixed Shadow observer monitor/stop commands.")


def _render_market_matrix(rows: Iterable[Mapping[str, object]]) -> None:
    st.subheader("Frozen 20-market matrix")
    values = tuple(rows)
    if len(values) != 20:
        st.warning("The frozen 20-market configuration is unavailable.")
        return
    st.dataframe(
        [
            {
                "Symbol": _text(row.get("symbol")),
                "Asset class": _text(row.get("asset_class")),
                "EPIC": _text(row.get("epic")),
                "Market data": _text(row.get("state")),
                "Reason": _text(row.get("reason")),
            }
            for row in values
        ],
        hide_index=True,
        width="stretch",
    )


def _render_provider_health(rows: Iterable[Mapping[str, object]]) -> None:
    st.subheader("Provider health")
    values = tuple(rows)
    if not values:
        st.info("No provider-health observations are available.")
        return
    st.dataframe(
        [
            {
                "Observed UTC": _text(row.get("observed_at_utc")),
                "Provider": _text(row.get("provider")),
                "Status": _text(row.get("status")),
            }
            for row in values
        ],
        hide_index=True,
        width="stretch",
    )


def _render_epoch_readiness(rows: Iterable[Mapping[str, object]]) -> None:
    st.subheader("Pre-epoch readiness evidence")
    values = tuple(rows)
    if not values:
        st.info("No first read-only snapshot has been recorded for the human epoch review.")
        return
    st.dataframe([_scalar_row(row) for row in values], hide_index=True, width="stretch")


def _render_market_snapshots(rows: Iterable[Mapping[str, object]]) -> None:
    st.subheader("Market snapshots")
    values = tuple(rows)
    if not values:
        st.info("No post-epoch market snapshots are available.")
        return
    st.dataframe(
        [
            {
                "Decision UTC": _text(row.get("decision_timestamp_utc")),
                "Instrument": _text(row.get("instrument")),
                "EPIC": _text(row.get("epic")),
                "Metadata health": _text(row.get("metadata_health")),
                "Live price feed": _text(row.get("live_quote_health")),
                "Last quote age (seconds)": row.get("last_quote_age_seconds"),
                "Live price feed status": _text(row.get("stream_connection_status")),
            }
            for row in values
        ],
        hide_index=True,
        width="stretch",
    )


def _render_engine_insights(rows: Iterable[Mapping[str, object]]) -> None:
    st.subheader("Latest engine opinions")
    values = tuple(rows)
    if not values:
        st.info("No post-epoch engine opinions are available.")
        return
    st.dataframe(
        [
            {
                "Decision UTC": _text(row.get("decision_timestamp_utc")),
                "Instrument": _text(row.get("instrument")),
                "Engine": _engine_label(row.get("engine_id")),
                "Opinion / context": _insight_state(row.get("insight")),
                "Reasons": _insight_reasons(row.get("insight")),
            }
            for row in values
        ],
        hide_index=True,
        width="stretch",
    )


def _render_decisions(rows: Iterable[Mapping[str, object]]) -> None:
    st.subheader("Latest shadow decisions")
    values = tuple(rows)
    if not values:
        st.info("No post-epoch shadow decisions are available.")
        return
    st.dataframe(
        [
            {
                "Decision UTC": _text(row.get("decision_timestamp_utc")),
                "Instrument": _text(row.get("instrument")),
                "Policy": _text(row.get("policy_id")),
                "Direction": _text(row.get("direction")),
                "Quality": _text(row.get("quality_state")),
                "Cost": _text(row.get("cost_state")),
                "Factors": _labels(row.get("factor_tags")),
                "Reasons": _labels(row.get("reason_codes")),
            }
            for row in values
        ],
        hide_index=True,
        width="stretch",
    )


def _render_resolved_outcomes(rows: Iterable[Mapping[str, object]]) -> None:
    st.subheader("Resolved future outcomes")
    values = tuple(rows)
    if not values:
        st.info("No delayed outcome labels are resolved yet.")
        return
    st.dataframe(
        [
            {
                "Decision UTC": _text(row.get("decision_timestamp_utc")),
                "Instrument": _text(row.get("instrument")),
                "Policy": _text(row.get("policy_id")),
                "Horizon sessions": row.get("horizon_sessions"),
                "Outcome UTC": _text(row.get("outcome_timestamp_utc")),
                "Data health": _text(row.get("quality")),
                "Directional return": row.get("raw_directional_return"),
                "ATR-normalized return": row.get("atr_normalized_return"),
                "Blocked reason": _text(row.get("blocked_reason")),
            }
            for row in values
        ],
        hide_index=True,
        width="stretch",
    )


def _render_leaderboard(rows: Iterable[Mapping[str, object]]) -> None:
    st.subheader("Outcome leaderboard")
    values = tuple(rows)
    if not values:
        st.info("No resolved outcome labels are available.")
        return
    st.dataframe([_scalar_row(row) for row in values], hide_index=True, width="stretch")


def _render_factor_audit(rows: Iterable[Mapping[str, object]]) -> None:
    st.subheader("Factor exposure map and correlation audit")
    values = tuple(rows)
    if not values:
        st.info("No factor-audit rows are available.")
        return
    st.dataframe([_scalar_row(row) for row in values], hide_index=True, width="stretch")


def _scalar_row(row: Mapping[str, object]) -> dict[str, str | int | float | bool | None]:
    result: dict[str, str | int | float | bool | None] = {}
    for key, value in row.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key.replace("_", " ").title()] = value
    return result


def _labels(value: object) -> str:
    if not isinstance(value, list):
        return "NOT AVAILABLE"
    return ", ".join(_text(item) for item in value if isinstance(item, str)) or "NOT AVAILABLE"


def _engine_label(value: object) -> str:
    labels = {
        "TECHNICAL_STATE": "Technical state",
        "T1": "Trend opinion",
        "M1": "Reversion opinion",
        "X1": "Cross-market context",
        "F1": "Fundamental context",
        "Q1": "Data health",
        "C1": "Estimated cost context",
    }
    return labels.get(value, "NOT AVAILABLE") if isinstance(value, str) else "NOT AVAILABLE"


def _insight_state(value: object) -> str:
    if not isinstance(value, Mapping):
        return "NOT AVAILABLE"
    for key in ("direction", "state", "trend_strength", "strength", "percentile"):
        candidate = value.get(key)
        if isinstance(candidate, (str, int, float)) and not isinstance(candidate, bool):
            return str(candidate)[:160]
    return "RECORDED"


def _insight_reasons(value: object) -> str:
    return _labels(value.get("reason_codes")) if isinstance(value, Mapping) else "NOT AVAILABLE"


def _text(value: object) -> str:
    if not isinstance(value, str):
        return "NOT AVAILABLE"
    compact = value.strip()
    return compact[:160] if compact else "NOT AVAILABLE"
