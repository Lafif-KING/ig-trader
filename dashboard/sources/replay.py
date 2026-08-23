"""Validated historical replay summary; not a live trading result."""

from __future__ import annotations


def historical_replay_summary() -> dict[str, str | int]:
    """Return the approved G3B summary without deriving a profitability claim."""

    return {
        "label": "Historical offline replay — not live Shadow results.",
        "decisions": 1917,
        "candidates": 20,
        "trades": 4,
        "wins": 0,
        "losses": 4,
        "pips": "-16 pips",
        "r_multiple": "-4R",
    }
