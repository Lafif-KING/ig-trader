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
from dashboard.sources.github import (
    ANONYMOUS_CACHE_TTL_SECONDS,
    TOKEN_CACHE_TTL_SECONDS,
    fetch_github_status,
    has_github_token,
)
from dashboard.sources.project import ProjectGateValidationError, load_project_status
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
    """Render a page from reviewed gates and optional safe test data."""

    try:
        project_status = load_project_status()
    except ProjectGateValidationError:
        st.error("PROJECT GATE DATA UNAVAILABLE")
        st.stop()
    github_status = github if github is not None else load_github_status()
    if page == "Overview":
        overview.render(project_status.summary, project_status.gates, github_status)
    elif page == "Project Roadmap":
        roadmap.render(project_status.gates)
    elif page == "Tests & GitHub CI":
        tests_ci.render(github_status)
    elif page == "Shadow Results":
        shadow.render(load_shadow_data(), historical_replay_summary())
    elif page == "Demo Results":
        demo.render()
    elif page == "Live Results":
        live.render()
    else:
        system.render(project_status.summary)


def main() -> None:
    """Start browser-only dashboard; no trading or cloud client is constructed."""

    configure_page()
    st.title("IG Trader Control Center")
    page = st.sidebar.radio("Navigation", PAGES)
    if st.sidebar.button("Refresh public GitHub status"):
        clear_github_status_cache()

    @st.fragment(run_every=60)
    def render_current_page() -> None:
        render_control_center(page)

    render_current_page()


if __name__ == "__main__":
    main()
