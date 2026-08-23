"""Overview page."""

from __future__ import annotations

import streamlit as st

from dashboard.components import render_safety_banner, render_summary_cards
from dashboard.models import GitHubStatus, ProjectGate, ProjectSummary
from dashboard.status import display_status, weighted_progress


def render(summary: ProjectSummary, gates: tuple[ProjectGate, ...], github: GitHubStatus) -> None:
    st.header("Overview")
    st.write(
        "A read-only project view. It cannot start a worker, change a gate, or place an order."
    )
    render_safety_banner(summary)
    foundation = tuple(gate for gate in gates if gate.group == "FOUNDATION")
    authority = tuple(gate for gate in gates if gate.group in {"SHADOW", "DEMO", "LIVE"})
    workflow = github.latest_workflow.display_result if github.latest_workflow else "UNAVAILABLE"
    render_summary_cards(
        (
            (
                "Current project phase",
                f"{summary.current_phase} — {display_status(summary.current_status)}",
                "Reviewed governance data.",
            ),
            (
                "Engineering foundation progress",
                f"{weighted_progress(foundation)}%",
                "Foundation technical evidence.",
            ),
            (
                "Trading-authority readiness",
                f"{weighted_progress(authority, authority=True)}%",
                "Disabled or unauthorized gates receive no credit.",
            ),
            (
                "Current governance",
                summary.real_database_governance,
                "GitHub cannot change reviewed governance.",
            ),
            (
                "Current main SHA",
                github.main_sha or "GITHUB DATA TEMPORARILY UNAVAILABLE",
                "Public GitHub metadata; 60 seconds with a token or 300 seconds anonymously.",
            ),
            ("Latest CI result", workflow, "Technical evidence, not trading approval."),
            (
                "Current blocker",
                summary.current_blocker,
                "Reviewed project summary.",
            ),
            (
                "Exact next action",
                summary.next_action,
                "Reviewed project summary; no dashboard action is available.",
            ),
            (
                "Last successful update",
                summary.last_verified_at.isoformat(),
                "Reviewed project summary.",
            ),
            ("Execution mode", summary.execution_mode, "The dashboard has no broker authority."),
            (
                "Broker authority",
                summary.broker_order_authority,
                "No broker client is imported or created.",
            ),
            (
                "Demo authority",
                display_status(summary.demo_execution),
                "No page can enable Demo execution.",
            ),
            (
                "Live authority",
                display_status(summary.live_execution),
                "No page can enable Live execution.",
            ),
        )
    )
    st.caption(
        "GitHub provides detected technical evidence only; project/gates.json controls visible "
        "authority."
    )
