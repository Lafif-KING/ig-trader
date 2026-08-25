"""Read-only Strategy Lab evidence page."""

from __future__ import annotations

import streamlit as st

from dashboard.components import render_summary_cards
from dashboard.sources.strategy_lab import StrategyLabSnapshot


def render(snapshot: StrategyLabSnapshot) -> None:
    st.header("Strategy Lab")
    st.write(
        "Local, broker-neutral research evidence. This page has no execution controls and cannot "
        "promote a strategy to Demo or Live trading."
    )
    if not snapshot.available:
        st.info("STRATEGY LAB ARTIFACTS NOT AVAILABLE")
        st.caption(
            "Run the offline Strategy Lab CLI with local fixtures to generate reviewed evidence."
        )
        return
    instrument_summary = snapshot.instrument_summary or {}
    strategy_summary = snapshot.strategy_summary or {}
    dataset_status = instrument_summary.get("dataset_status", {})
    dq03_verified = sum(item.get("classification") == "VERIFIED" for item in snapshot.dq03_metadata)
    render_summary_cards(
        (
            (
                "Instrument count",
                str(instrument_summary.get("instrument_count", 0)),
                "Local artifact evidence.",
            ),
            (
                "Datasets available",
                _count(dataset_status, "available"),
                "Local evidence only.",
            ),
            (
                "DQ-03 contracts",
                str(dq03_verified),
                "Verified metadata is not a research dataset or trading permit.",
            ),
            (
                "Strategies tested",
                str(strategy_summary.get("strategies_tested", 0)),
                "Evidence count.",
            ),
            (
                "Combinations tested",
                str(strategy_summary.get("combinations_tested", 0)),
                "Evidence count.",
            ),
            (
                "Champion candidates",
                str(strategy_summary.get("champion_candidates", 0)),
                "Not a promotion.",
            ),
            (
                "Challengers",
                str(strategy_summary.get("challengers", 0)),
                "Retained for comparison.",
            ),
            (
                "Rejected combinations",
                str(strategy_summary.get("rejected", 0)),
                "Conservative research status.",
            ),
            (
                "Insufficient-data combinations",
                str(strategy_summary.get("insufficient_data", 0)),
                "No missing data is invented.",
            ),
            (
                "Pre-simulation blockers",
                str(strategy_summary.get("pre_simulation_blocked", 0)),
                "Not a negative backtest result.",
            ),
            (
                "Simulated failures",
                str(strategy_summary.get("simulated_and_failed", 0)),
                "Backtested but did not qualify.",
            ),
        )
    )
    if snapshot.sl04_before_after:
        st.subheader("Deep-data coverage comparison")
        before = snapshot.sl04_before_after.get("before", {})
        after = snapshot.sl04_before_after.get("after", {})
        st.dataframe(
            [
                {
                    "Evidence": "SL-03 previous data",
                    "Datasets": before.get("datasets"),
                    "Scheduled": before.get("scheduled_combinations"),
                    "Simulated": before.get("simulated_combinations"),
                    "Simulation coverage %": before.get("simulation_percentage"),
                    "Block counts": before.get("block_counts"),
                },
                {
                    "Evidence": "SL-04 deep data",
                    "Datasets": after.get("datasets"),
                    "Scheduled": after.get("scheduled_combinations"),
                    "Simulated": after.get("simulated_combinations"),
                    "Simulation coverage %": after.get("simulation_percentage"),
                    "Block counts": after.get("block_counts"),
                },
            ],
            hide_index=True,
            width="stretch",
        )
    filtered = _filters(snapshot.entries)
    st.subheader("Leaderboard")
    st.dataframe(
        [
            {
                "Instrument": row["instrument"],
                "IG EPIC": row.get("ig_epic"),
                "Asset class": row["asset_class"],
                "Strategy": row["strategy"],
                "Version": row["version"],
                "Timeframe": row["timeframe"],
                "Data source": row.get("data_source"),
                "Data duration seconds": row.get("data_duration_seconds"),
                "Data rows": row.get("candle_count"),
                "Data depth": row.get("data_depth"),
                "Data quality": row.get("data_quality"),
                "IG alignment": row.get("alignment_status"),
                "Raw signals": row.get("raw_signals"),
                "Trades": row["trades"],
                "OOS trades": row.get("oos_trades"),
                "Win rate": row.get("win_rate"),
                "Net R": row.get("net_r"),
                "Expectancy": row.get("expectancy"),
                "Profit factor": row.get("profit_factor"),
                "Max drawdown": row.get("max_drawdown"),
                "OOS expectancy": row.get("oos_expectancy"),
                "+25% stress": _stress_expectancy(row, "1.25"),
                "+50% stress": _stress_expectancy(row, "1.50"),
                "Evaluation state": row.get("evaluation_state"),
                "Classification": row.get("classification", row["status"]),
                "Rank": row.get("champion_challenger_rank"),
                "Why rejected": row.get("why_rejected"),
                "Demo-ready": row.get("demo_ready", False),
            }
            for row in filtered
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "Champion status is research evidence only; project governance controls every promotion."
    )
    if snapshot.dq03_metadata:
        st.subheader("DQ-03 broker metadata")
        st.dataframe(snapshot.dq03_metadata, hide_index=True, width="stretch")
        st.caption(
            "DQ-03 uses read-only IG metadata. Missing broad research data remains "
            "DATA_NOT_AVAILABLE."
        )


def _filters(entries: tuple[dict[str, object], ...]) -> tuple[dict[str, object], ...]:
    options = {
        name: ("All",) + tuple(sorted({str(item[name]) for item in entries}))
        for name in ("asset_class", "instrument", "strategy", "timeframe", "status")
    }
    columns = st.columns(5)
    selections = {
        name: column.selectbox(name.replace("_", " ").title(), options[name])
        for column, name in zip(columns, options, strict=True)
    }
    return tuple(
        item
        for item in entries
        if all(
            selection == "All" or item[name] == selection for name, selection in selections.items()
        )
    )


def _count(value: object, name: str) -> str:
    return str(value.get(name, 0)) if isinstance(value, dict) else "0"


def _stress_expectancy(row: dict[str, object], multiplier: str) -> object:
    scenarios = row.get("stress_scenarios")
    if not isinstance(scenarios, list):
        return None
    for scenario in scenarios:
        if isinstance(scenario, dict) and str(scenario.get("cost_multiplier")) == multiplier:
            return scenario.get("expectancy")
    return None
