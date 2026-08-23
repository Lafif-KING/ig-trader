"""Locked Demo page."""

from __future__ import annotations

import streamlit as st


def render() -> None:
    st.header("Demo Results")
    st.error("DEMO_EXECUTION DISABLED")
    st.write("This is a locked information page. There is no control to enable Demo execution.")
    st.subheader("Prerequisites")
    for item in (
        "Shadow engineering qualification",
        "Demo execution adapter",
        "Broker order lifecycle tests",
        "Broker reconciliation tests",
        "Explicit project approval",
    ):
        st.write(f"- {item}")
