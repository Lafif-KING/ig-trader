"""Streamlit entry point for the read-only IG Trader Control Center."""

from __future__ import annotations

import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st

from dashboard.components import configure_page
from dashboard.models import GitHubStatus
from dashboard.pages import (
    control_center,
    demo,
    demo_operator,
    live,
    overview,
    roadmap,
    shadow,
    strategy_lab,
    system,
    tests_ci,
)
from dashboard.sources.control_center import load_control_center_state
from dashboard.sources.demo_operator import load_demo_operator_snapshot
from dashboard.sources.github import (
    ANONYMOUS_CACHE_TTL_SECONDS,
    TOKEN_CACHE_TTL_SECONDS,
    fetch_github_status,
    has_github_token,
)
from dashboard.sources.project import ProjectGateValidationError, load_project_status
from dashboard.sources.replay import historical_replay_summary
from dashboard.sources.shadow import load_shadow_data
from dashboard.sources.strategy_lab import load_strategy_lab_snapshot

PAGES = (
    "COCKPIT",
    "MARKET SCANNER",
    "STRATEGY CENTER",
    "POSITIONS",
    "PERFORMANCE",
    "DECISION EXPLORER",
    "RISK & HEALTH",
    "DEMO OPERATOR",
)


@st.cache_data(ttl=TOKEN_CACHE_TTL_SECONDS, show_spinner=False)
def _load_github_status_with_token() -> GitHubStatus:
    """Cache authenticated public metadata for the shorter token-aware interval."""

    if os.environ.get("DASHBOARD_GITHUB_OFFLINE") == "1":
        return GitHubStatus(available=False)
    return fetch_github_status()


@st.cache_data(ttl=ANONYMOUS_CACHE_TTL_SECONDS, show_spinner=False)
def _load_github_status_anonymously() -> GitHubStatus:
    """Cache anonymous public metadata conservatively to avoid API-limit churn."""

    if os.environ.get("DASHBOARD_GITHUB_OFFLINE") == "1":
        return GitHubStatus(available=False)
    return fetch_github_status()


def load_github_status() -> GitHubStatus:
    """Choose cache policy by token presence without exposing the credential."""

    if has_github_token():
        return _load_github_status_with_token()
    return _load_github_status_anonymously()


def clear_github_status_cache() -> None:
    """Refresh only GitHub metadata; reviewed project data remains unchanged."""

    _load_github_status_with_token.clear()
    _load_github_status_anonymously.clear()


def render_control_center(page: str, github: GitHubStatus | None = None) -> None:
    """Render navigation from prepared read-only state and retained legacy views."""

    try:
        project_status = load_project_status()
    except ProjectGateValidationError:
        st.error("PROJECT GATE DATA UNAVAILABLE")
        st.stop()
    snapshot = load_demo_operator_snapshot()
    state = load_control_center_state(project_status, snapshot)
    if page == "COCKPIT":
        control_center.render_cockpit(state)
    elif page == "MARKET SCANNER":
        control_center.render_market_scanner(state)
    elif page == "STRATEGY CENTER":
        control_center.render_strategy_center(state)
    elif page == "POSITIONS":
        control_center.render_positions(state)
    elif page == "PERFORMANCE":
        control_center.render_performance(state)
    elif page == "DECISION EXPLORER":
        control_center.render_decision_explorer(state)
    elif page == "RISK & HEALTH":
        control_center.render_risk_health(state)
    elif page == "DEMO OPERATOR":
        demo_operator.render(snapshot, state)
    # Retained programmatic legacy views stay read-only but are intentionally
    # not part of the MVP navigation.
    elif page == "Overview":
        overview.render(
            project_status.summary,
            project_status.gates,
            github if github is not None else load_github_status(),
        )
    elif page == "Project Roadmap":
        roadmap.render(project_status.gates)
    elif page == "Strategy Lab":
        strategy_lab.render(load_strategy_lab_snapshot())
    elif page == "Tests & GitHub CI":
        tests_ci.render(github if github is not None else load_github_status())
    elif page == "Shadow Results":
        shadow.render(load_shadow_data(), historical_replay_summary())
    elif page == "Demo Results":
        demo.render()
    elif page == "Demo Operator":
        demo_operator.render(snapshot, state)
    elif page == "Live Results":
        live.render()
    else:
        system.render(project_status.summary)


def main() -> None:
    """Start browser-only dashboard; no trading or cloud client is constructed."""

    configure_page()
    st.title("IG Trader Control Center")
    st.caption("Local operator console. Opening it does not start a robot or send an order.")
    page = st.sidebar.radio("Navigation", PAGES)
    if st.sidebar.button("Refresh public GitHub status"):
        clear_github_status_cache()

    @st.fragment(run_every=60)
    def render_current_page() -> None:
        try:
            project_status = load_project_status()
            state = load_control_center_state(project_status, load_demo_operator_snapshot())
        except ProjectGateValidationError:
            st.error("PROJECT GATE DATA UNAVAILABLE")
            return
        control_center.render_simulation_banner(state)
        render_control_center(page)

    render_current_page()


if __name__ == "__main__":
    main()
