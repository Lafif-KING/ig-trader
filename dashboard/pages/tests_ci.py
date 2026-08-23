"""Tests and GitHub CI page."""

from __future__ import annotations

import streamlit as st

from dashboard.components import render_pr_list, render_workflow
from dashboard.models import GitHubStatus


def render(github: GitHubStatus) -> None:
    st.header("Tests & GitHub CI")
    st.write(
        "This page reads public GitHub metadata only. It never reads workflow logs or secrets."
    )
    if not github.available:
        st.warning("GITHUB DATA TEMPORARILY UNAVAILABLE")
        st.caption("The static Project Roadmap remains available while GitHub cannot be reached.")
        return
    st.write(f"**Current main SHA:** `{github.main_sha or 'Not reported'}`")
    st.write(f"**Main last updated:** {github.main_updated_at or 'Not reported'}")
    if github.latest_workflow is None:
        st.info(f"{github.workflow_context}: GitHub did not report a workflow run.")
    else:
        render_workflow(github.latest_workflow, github.workflow_context)
    render_pr_list("Open pull requests", github.open_pull_requests)
    render_pr_list("Recently merged pull requests", github.merged_pull_requests)
