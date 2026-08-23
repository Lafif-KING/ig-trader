"""Overview page."""

from __future__ import annotations

import streamlit as st

from dashboard.components import render_safety_banner, render_summary_cards
from dashboard.models import GitHubStatus, ProjectGate
from dashboard.status import weighted_progress


def render(gates: tuple[ProjectGate, ...], github: GitHubStatus) -> None:
    st.header("Overview")
    st.write(
        "A read-only project view. It cannot start a worker, change a gate, or place an order."
    )
    render_safety_banner()
    foundation = tuple(gate for gate in gates if gate.group == "FOUNDATION")
    authority = tuple(gate for gate in gates if gate.group in {"SHADOW", "DEMO", "LIVE"})
    workflow = github.latest_workflow.display_result if github.latest_workflow else "UNAVAILABLE"
    render_summary_cards(
        (
            ("Current project phase", "DASHBOARD MVP — IN PROGRESS", "Observability only."),
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
            ("Current governance", "NO EXECUTION AUTHORITY", "GitHub cannot change this status."),
            (
                "Current main SHA",
                github.main_sha or "GITHUB DATA TEMPORARILY UNAVAILABLE",
                "Public GitHub metadata; cached for up to 60 seconds.",
            ),
            ("Latest CI result", workflow, "Technical evidence, not trading approval."),
            ("Current blocker", "DATABASE RECOVERY HOLD", "Real migration 003 state is UNKNOWN."),
            (
                "Exact next action",
                "VALIDATE DATABASE STATE",
                "Separately authorized recovery procedure.",
            ),
            (
                "Last successful update",
                max(gate.last_verified_at for gate in gates).isoformat(),
                "Reviewed project gates.",
            ),
            ("Execution mode", "NO_EXECUTION", "The dashboard has no broker authority."),
            ("Broker authority", "OFF", "No broker client is imported or created."),
            ("Demo authority", "DISABLED", "No page can enable Demo execution."),
            ("Live authority", "DISABLED", "No page can enable Live execution."),
        )
    )
    st.caption(
        "GitHub provides detected technical evidence only; project/gates.json controls visible "
        "authority."
    )
