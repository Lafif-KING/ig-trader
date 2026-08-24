"""Safe local artifact reader for the read-only Strategy Lab dashboard page."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT_DIRECTORY = ROOT / "artifacts" / "strategy_lab"
DEFAULT_DQ03_METADATA_PATH = ROOT / "artifacts" / "dq03" / "metadata_summary.json"


@dataclass(frozen=True)
class StrategyLabSnapshot:
    available: bool
    entries: tuple[dict[str, object], ...] = ()
    instrument_summary: dict[str, object] | None = None
    strategy_summary: dict[str, object] | None = None
    dq03_metadata: tuple[dict[str, object], ...] = ()


def load_strategy_lab_snapshot(
    artifact_directory: Path = DEFAULT_ARTIFACT_DIRECTORY,
) -> StrategyLabSnapshot:
    """Read local generated evidence only; absence remains visible and safe."""

    leaderboard = _load_object(artifact_directory / "sl02_leaderboard.json") or _load_object(
        artifact_directory / "leaderboard.json"
    )
    if leaderboard is None or not isinstance(leaderboard.get("entries"), list):
        return StrategyLabSnapshot(available=False)
    entries = tuple(_normalise_entry(item) for item in leaderboard["entries"] if _safe_entry(item))
    instrument_summary = _load_object(artifact_directory / "instrument_summary.json")
    strategy_summary = _load_object(artifact_directory / "strategy_summary.json")
    if instrument_summary is None and (artifact_directory / "sl02_leaderboard.json").is_file():
        instrument_summary = {
            "instrument_count": len({str(item["instrument"]) for item in entries}),
            "dataset_status": {
                "available": sum(item.get("dataset_fingerprint") is not None for item in entries),
                "not_available": sum(item.get("dataset_fingerprint") is None for item in entries),
            },
        }
    if strategy_summary is None and (artifact_directory / "sl02_leaderboard.json").is_file():
        strategy_summary = {
            "strategies_tested": len({str(item["strategy"]) for item in entries}),
            "combinations_tested": len(entries),
            "champion_candidates": sum(
                item.get("champion_challenger_rank") == "CHAMPION_CANDIDATE" for item in entries
            ),
            "challengers": sum(
                str(item.get("champion_challenger_rank", "")).startswith("CHALLENGER_")
                for item in entries
            ),
            "rejected": sum(
                item.get("classification")
                in {
                    "RESEARCH_REJECTED",
                    "NEGATIVE_EXPECTANCY",
                    "OVERFIT_RISK",
                    "STRESS_TEST_FAIL",
                    "SOURCE_DIVERGENCE",
                }
                for item in entries
            ),
            "insufficient_data": sum(
                item.get("classification")
                in {
                    "DATA_NOT_AVAILABLE",
                    "DATA_QUALITY_FAIL",
                    "LOW_DATA_DEPTH",
                    "COST_MODEL_INCOMPLETE",
                    "INSUFFICIENT_TRADES",
                }
                for item in entries
            ),
            "pre_simulation_blocked": sum(
                item.get("evaluation_state") == "PRE_SIMULATION_BLOCKED" for item in entries
            ),
            "simulated_and_failed": sum(
                item.get("evaluation_state") == "SIMULATED_AND_FAILED" for item in entries
            ),
        }
    return StrategyLabSnapshot(
        available=True,
        entries=entries,
        instrument_summary=instrument_summary,
        strategy_summary=strategy_summary,
        dq03_metadata=_load_dq03_metadata(),
    )


def _load_object(path: Path) -> dict[str, object] | None:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_entry(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    required = {
        "instrument",
        "asset_class",
        "strategy",
        "timeframe",
    }
    primitive = str | int | float | type(None)
    version_is_present = isinstance(value.get("version") or value.get("strategy_version"), str)
    trade_count_is_present = isinstance(value.get("trades", value.get("trade_count")), int)
    status_is_present = isinstance(value.get("status", value.get("classification")), str)
    return (
        required.issubset(value)
        and version_is_present
        and trade_count_is_present
        and status_is_present
        and all(isinstance(value[field], primitive) for field in required)
    )


def _normalise_entry(value: dict[str, object]) -> dict[str, object]:
    """Give SL-01 and SL-02 evidence one safe dashboard shape."""

    return {
        **value,
        "version": value.get("version", value.get("strategy_version")),
        "trades": value.get("trades", value.get("trade_count")),
        "status": value.get("status", value.get("classification")),
    }


def _load_dq03_metadata() -> tuple[dict[str, object], ...]:
    document = _load_object(DEFAULT_DQ03_METADATA_PATH)
    values = document.get("instruments") if document else None
    if not isinstance(values, list):
        return ()
    return tuple(
        item
        for item in values
        if isinstance(item, dict)
        and isinstance(item.get("symbol"), str)
        and isinstance(item.get("classification"), str)
    )
