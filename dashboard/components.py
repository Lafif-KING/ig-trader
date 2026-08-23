"""Shared plain-English presentation components for the Control Center."""

from __future__ import annotations

from collections.abc import Iterable

import streamlit as st

from dashboard.models import ProjectGate, ProjectSummary, WorkflowRun
from dashboard.status import display_status


def configure_page() -> None:
    st.set_page_config(page_title="IG Trader Control Center", page_icon="🛡️", layout="wide")
    st.markdown(
        "<style>.block-container{max-width:1280px;padding-top:2rem;padding-bottom:3rem;}"
        "[data-testid='stMetric']{border:1px solid #d9e2ec;"
        "border-radius:.7rem;padding:.8rem;}</style>",
        unsafe_allow_html=True,
    )


def render_safety_banner(summary: ProjectSummary) -> None:
    st.error(
        f"EXECUTION MODE: {summary.execution_mode}\n\n"
        f"BROKER ORDER AUTHORITY: {summary.broker_order_authority}\n\n"
        f"DEMO EXECUTION: {display_status(summary.demo_execution)}\n\n"
        f"LIVE EXECUTION: {display_status(summary.live_execution)}\n\n"
        f"REAL AZURE DATABASE: {summary.real_database_governance.replace('_', ' ')} / "
        f"STATE {display_status(summary.real_database_state)}"
    )


def render_summary_cards(items: Iterable[tuple[str, str, str | None]]) -> None:
    card_items = tuple(items)
    for start in range(0, len(card_items), 3):
        row_items = card_items[start : start + 3]
        for column, (label, value, help_text) in zip(
            st.columns(len(row_items)), row_items, strict=True
        ):
            with column:
                st.metric(label, value, help=help_text)


def render_gate(gate: ProjectGate) -> None:
    with st.expander(
        f"{gate.gate_id} — {gate.display_name} — {display_status(gate.governance_status)}"
    ):
        st.write(gate.plain_english_description)
        left, right = st.columns(2)
        left.write(f"**Governance status:** {display_status(gate.governance_status)}")
        left.write(f"**Technical evidence:** {display_status(gate.technical_status)}")
        left.write(f"**Owner:** {gate.owner}")
        right.write(f"**Completed SHA:** {gate.completed_sha or 'Not completed'}")
        right.write(f"**Related PR:** {gate.related_pr or 'Not recorded'}")
        right.write(f"**Last verified:** {gate.last_verified_at.isoformat()}")
        st.write(f"**Blocker:** {gate.blocker or 'None recorded'}")
        st.write(f"**Next action:** {gate.next_action}")
        st.write("**Evidence:**")
        for evidence in gate.evidence:
            st.write(f"- {evidence}")


def render_workflow(workflow: WorkflowRun, context: str) -> None:
    st.subheader(f"Latest workflow: {workflow.display_result} — {context}")
    st.write(
        f"**{workflow.name} #{workflow.number}** tested commit `{workflow.head_sha}` "
        f"on branch `{workflow.branch}`."
    )
    st.write(
        f"**Pull request:** #{workflow.pull_request}"
        if workflow.pull_request
        else "**Pull request:** Not reported"
    )
    if workflow.display_result == "PASS":
        st.success("GitHub tested the complete project successfully.")
    elif workflow.display_result == "FAIL":
        st.error(f"GitHub found a problem in the step: {workflow.first_failed_step or 'unnamed'}.")
    else:
        st.info("GitHub has not reported a final result yet.")
    render_summary_cards(
        (
            ("Passed steps", str(workflow.passed_steps), "Steps GitHub reported as successful."),
            ("Failed steps", str(workflow.failed_steps), "Steps GitHub reported as failed."),
            ("Skipped steps", str(workflow.skipped_steps), "Steps GitHub did not need to run."),
        )
    )
    st.write(f"**First failed step:** {workflow.first_failed_step or 'None reported'}")
    st.write(f"**Safe failure summary:** {workflow.failure_summary or 'No failure summary.'}")
    st.write(f"**Started:** {workflow.started_at or 'Not reported'}")
    st.write(f"**Completed:** {workflow.completed_at or 'Not reported'}")
    st.link_button("Open GitHub Actions", workflow.url, type="secondary")


def render_pr_list(title: str, pull_requests: Iterable[object]) -> None:
    st.subheader(title)
    items = tuple(pull_requests)
    if not items:
        st.caption("None reported by the public GitHub snapshot.")
        return
    for item in items:
        st.write(f"[#{item.number} — {item.title}]({item.url})")
