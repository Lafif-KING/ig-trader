"""System and safety page."""

from __future__ import annotations

import streamlit as st

from dashboard.components import render_safety_banner


def render() -> None:
    st.header("System & Safety")
    render_safety_banner()
    st.subheader("Current known safety state")
    for label, value in (
        ("Broker authority", "OFF"),
        ("Order authority", "OFF"),
        ("Broker order call count", "0 in validated Shadow engineering tests"),
        ("Real database recovery", "HOLD"),
        ("Azure Shadow worker", "NOT DEPLOYED"),
        ("Temporary Azure recovery resources", "RETAIN UNTIL DB VALIDATION"),
    ):
        st.write(f"**{label}:** {value}")
    st.subheader("Project safety priority")
    st.write("SAFETY → CORRECTNESS → OBSERVABILITY → RELIABILITY → PERFORMANCE → SOPHISTICATION")
