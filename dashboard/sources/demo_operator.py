"""Safe file-only source for the local Demo Operator dashboard page.

This module never imports broker code or opens the SQLite store.  A separate
local worker publishes a deliberately sanitized JSON snapshot for the UI.
"""

# ruff: noqa: E501

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SNAPSHOT_PATH = ROOT / ".runtime" / "demo_operator" / "operator_snapshot.json"

_STRATEGIES = {
    "S0": {
        "name": "Frozen RSI/ADX reference",
        "version": "frozen-v1-reference",
        "description": "Frozen comparison baseline. It is not optimized or automatically promoted.",
        "entry": "Completed-candle RSI/ADX reference condition.",
        "exit": "Existing frozen evidence only.",
        "weaknesses": "A small historical reference can be unrepresentative.",
    },
    "S1": {
        "name": "Trend momentum",
        "version": "1.0.0",
        "description": "Evaluates whether a completed directional move continues.",
        "entry": "Completed price structure and direction agree.",
        "exit": "Reviewed volatility-aware stop and target evidence.",
        "weaknesses": "Range-bound markets can create false continuations.",
    },
    "S2": {
        "name": "Range breakout",
        "version": "1.0.0",
        "description": "Evaluates a completed close beyond a recent range.",
        "entry": "Completed close beyond the observed range boundary.",
        "exit": "Reviewed invalidation and reward evidence.",
        "weaknesses": "False breaks and slippage can be material.",
    },
    "S3": {
        "name": "Mean reversion",
        "version": "1.0.0",
        "description": "Evaluates a return toward a recent average after a stretch.",
        "entry": "Completed volatility-normalized price stretch.",
        "exit": "Range invalidation or documented return objective.",
        "weaknesses": "Strong trends can continue rather than revert.",
    },
    "S4": {
        "name": "Session sweep",
        "version": "1.0.0",
        "description": "Evaluates a range sweep that closes back inside its range.",
        "entry": "Completed sweep and close back through a boundary.",
        "exit": "Reviewed structural invalidation and return objective.",
        "weaknesses": "News can turn a sweep into a true breakout.",
    },
    "S5": {
        "name": "Volatility regime",
        "version": "1.0.0",
        "description": "Evaluates directional movement during observed volatility expansion.",
        "entry": "Recent volatility exceeds the documented normal range.",
        "exit": "Reviewed volatility-aware stop and target evidence.",
        "weaknesses": "Short-lived spikes can reverse sharply.",
    },
    "S6": {
        "name": "Price structure",
        "version": "1.0.0",
        "description": "Evaluates a decisive completed break of a recent swing.",
        "entry": "Swing break with material completed-candle displacement.",
        "exit": "Reviewed structural invalidation and target evidence.",
        "weaknesses": "Choppy periods can produce weak structure breaks.",
    },
    "S7": {
        "name": "Multi-timeframe trend",
        "version": "1.0.0",
        "description": "Evaluates broader context and short-term trigger alignment.",
        "entry": "Completed context and trigger point in the same direction.",
        "exit": "Reviewed volatility-aware stop and target evidence.",
        "weaknesses": "Conflicting timeframes can invalidate an entry.",
    },
}

_INSTRUMENTS = (
    ("EURUSD", "FX", "S1"),
    ("GBPUSD", "FX", "S1"),
    ("EURGBP", "FX", "S3"),
    ("USDJPY", "FX", "S5"),
    ("EURJPY", "FX", "S5"),
    ("GBPJPY", "FX", "S5"),
    ("AUDUSD", "FX", "S1"),
    ("NZDUSD", "FX", "S1"),
    ("USDCAD", "FX", "S1"),
    ("USDCHF", "FX", "S1"),
    ("EURCHF", "FX", "S1"),
    ("EURAUD", "FX", "S1"),
    ("GBPAUD", "FX", "S1"),
    ("AUDJPY", "FX", "S5"),
    ("CADJPY", "FX", "S5"),
    ("CHFJPY", "FX", "S5"),
    ("XAUUSD", "METAL", "S2"),
    ("XAGUSD", "METAL", "S2"),
    ("GER40", "INDEX", "S4"),
    ("UK100", "INDEX", "S4"),
    ("US500", "INDEX", "S4"),
    ("USTECH100", "INDEX", "S4"),
    ("US30", "INDEX", "S4"),
    ("FRA40", "INDEX", "S4"),
    ("USCRUDE", "ENERGY", "S1"),
    ("BRENT", "ENERGY", "S1"),
)


@dataclass(frozen=True)
class DemoOperatorSnapshot:
    available: bool
    fields: dict[str, object]


def load_demo_operator_snapshot(path: Path = DEFAULT_SNAPSHOT_PATH) -> DemoOperatorSnapshot:
    """Read only the local worker's expected public operator fields."""

    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return DemoOperatorSnapshot(False, {})
    if not isinstance(document, dict):
        return DemoOperatorSnapshot(False, {})
    allowed = {
        "environment",
        "rest_status",
        "streaming_status",
        "robot_state",
        "account",
        "balance",
        "available_funds",
        "total_open_positions",
        "total_open_pnl",
        "today_realized_pnl",
        "last_successful_sync",
        "last_confirmation",
        "kill_switch_state",
        "message",
        "positions",
        "alerts",
    }
    values = {key: value for key, value in document.items() if key in allowed and _safe(value)}
    return DemoOperatorSnapshot(bool(values), values)


def strategy_catalog() -> dict[str, dict[str, str]]:
    return {
        key: {
            **value,
            "family": key,
            "market_hypothesis": "A testable market-behavior hypothesis, never a profitability claim.",
            "stop_logic": "Uses only broker-valid, strategy-recorded protective distances.",
            "target_logic": "Uses the reviewed research reward objective; no generic pip target.",
            "preferred_session": "Only an observed liquid session when current data is fresh.",
            "preferred_timeframe": "Research-defined completed-candle timeframe.",
            "preferred_regime": "Only the strategy's documented market regime.",
            "risk_considerations": "Current spread, stale data, account identity, and risk vetoes block entries.",
        }
        for key, value in _STRATEGIES.items()
    }


def research_instrument_rows() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "Instrument": symbol,
            "Asset class": asset_class,
            "IG EPIC": "Not discovered",
            "Market": "Not synchronized",
            "Spread": "Unavailable",
            "Assigned strategy": strategy,
            "Version": _STRATEGIES[strategy]["version"],
            "Timeframe": "Research-defined",
            "Strategy status": "RESEARCH",
            "Qualification": "DEMO_NOT_STARTED",
            "Current signal": "Unavailable",
            "Position": "No broker sync",
            "Direction": "—",
            "P&L": "Unavailable",
            "Historical trades": "Unavailable",
            "Historical Net R": "Unavailable",
            "OOS Expectancy": "Unavailable",
            "Demo trades": 0,
            "Demo P&L": "Unavailable",
            "Demo Net R": "Unavailable",
            "Last activity": "Not synchronized",
            "Why assigned": _why_assigned(symbol, strategy),
        }
        for symbol, asset_class, strategy in _INSTRUMENTS
    )


def _safe(value: object) -> bool:
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if isinstance(value, list):
        return all(_safe(item) for item in value)
    return False


def _why_assigned(symbol: str, strategy: str) -> str:
    if symbol == "EURGBP":
        return (
            "EUR/GBP is evaluated for range behavior. The mean-reversion hypothesis is disabled "
            "when trend or volatility evidence is strong."
        )
    return (
        f"This is a research-only {_STRATEGIES[strategy]['name']} assignment based on the reviewed "
        "instrument suitability matrix; it is not a Demo trading permit."
    )
