"""Shadow result page."""

from __future__ import annotations

import streamlit as st

from dashboard.models import ShadowDataStatus


def render(shadow: ShadowDataStatus, replay: dict[str, str | int]) -> None:
    st.header("Shadow Results")
    st.warning("SHADOW CLOUD DATA NOT AVAILABLE YET")
    st.write(f"**Reason:** {shadow.reason}")
    st.info(
        "Future broker-neutral data can provide decisions, signals, risk vetoes, intents, "
        "lifecycle, "
        "performance, and safety events. The current response is DATA_NOT_AVAILABLE."
    )
    st.subheader(str(replay["label"]))
    st.write(
        f"**{replay['trades']} trades** · **{replay['wins']} wins** · "
        f"**{replay['losses']} losses** · **{replay['pips']}** · **{replay['r_multiple']}**"
    )
    st.caption(
        f"Replay sample: {replay['decisions']} decisions and {replay['candidates']} candidates."
    )
    st.error("Four trades are not sufficient for a profitability conclusion.")
