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
from dashboard.pages import demo, live, overview, roadmap, shadow, system, tests_ci
from dashboard.sources.github import fetch_github_status
from dashboard.sources.project import ProjectGateValidationError, load_project_gates
from dashboard.sources.replay import historical_replay_summary
from dashboard.sources.shadow import load_shadow_data

PAGES = (
    "Overview",
    "Project Roadmap",
    "Tests & GitHub CI",
    "Shadow Results",
    "Demo Results",
    "Live Results",
    "System & Safety",
)


@st.cache_data(ttl=60, show_spinner=False)
def load_github_status() -> GitHubStatus:
    """Bound GitHub requests to a 60-second cache while the app session is open."""

    if os.environ.get("DASHBOARD_GITHUB_OFFLINE") == "1":
        return GitHubStatus(available=False)
    return fetch_github_status()


def render_control_center(page: str, github: GitHubStatus | None = None) -> None:
    """Render a page from reviewed gates and optional safe test data."""

    try:
        gates = load_project_gates()
    except ProjectGateValidationError:
        st.error("PROJECT GATE DATA UNAVAILABLE")
        st.stop()
    github_status = github if github is not None else load_github_status()
    if page == "Overview":
        overview.render(gates, github_status)
    elif page == "Project Roadmap":
        roadmap.render(gates)
    elif page == "Tests & GitHub CI":
        tests_ci.render(github_status)
    elif page == "Shadow Results":
        shadow.render(load_shadow_data(), historical_replay_summary())
    elif page == "Demo Results":
        demo.render()
    elif page == "Live Results":
        live.render()
    else:
        system.render()


def main() -> None:
    """Start browser-only dashboard; no trading or cloud client is constructed."""

    configure_page()
    st.title("IG Trader Control Center")
    page = st.sidebar.radio("Navigation", PAGES)
    if st.sidebar.button("Refresh public GitHub status"):
        load_github_status.clear()

    @st.fragment(run_every=60)
    def render_current_page() -> None:
        render_control_center(page)

    render_current_page()


if __name__ == "__main__":
    main()
