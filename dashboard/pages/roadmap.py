"""Reviewed project roadmap page."""

from __future__ import annotations

import streamlit as st

from dashboard.components import render_gate
from dashboard.models import ProjectGate
from dashboard.status import gates_for_group


def render(gates: tuple[ProjectGate, ...]) -> None:
    st.header("Project Roadmap")
    st.write("Each item separates approved governance from detected technical evidence.")
    for group in ("FOUNDATION", "SHADOW", "DEMO", "LIVE", "ENHANCEMENTS"):
        st.subheader(group.title())
        for gate in gates_for_group(gates, group):
            render_gate(gate)
