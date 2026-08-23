"""System and safety page."""

from __future__ import annotations

import streamlit as st

from dashboard.components import render_safety_banner
from dashboard.models import ProjectSummary


def render(summary: ProjectSummary) -> None:
    st.header("System & Safety")
    render_safety_banner(summary)
    st.subheader("Current known safety state")
    for label, value in (
        ("Broker authority", summary.broker_order_authority),
        ("Order authority", summary.broker_order_authority),
        ("Broker order call count", "0 in validated Shadow engineering tests"),
        ("Real database recovery", summary.real_database_governance),
        ("Azure Shadow worker", "NOT DEPLOYED"),
        ("Temporary Azure recovery resources", "RETAIN UNTIL DB VALIDATION"),
    ):
        st.write(f"**{label}:** {value}")
    st.subheader("Project safety priority")
    st.write("SAFETY → CORRECTNESS → OBSERVABILITY → RELIABILITY → PERFORMANCE → SOPHISTICATION")
