"""Locked Live page."""

from __future__ import annotations

import streamlit as st


def render() -> None:
    st.header("Live Results")
    st.error("LIVE_EXECUTION DISABLED")
    st.write("**Product-owner approval required:** Afif Ben Khedher")
    st.write("This is a locked information page. There is no control to enable Live execution.")
    st.subheader("Prerequisites")
    for item in (
        "Successful Demo qualification",
        "Validated contract sizes",
        "Validated pip values",
        "Final risk policy",
        "Kill switch",
        "Explicit live-risk approval",
    ):
        st.write(f"- {item}")
