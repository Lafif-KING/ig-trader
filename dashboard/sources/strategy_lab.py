"""Safe local artifact reader for the read-only Strategy Lab dashboard page."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT_DIRECTORY = ROOT / "artifacts" / "strategy_lab"


@dataclass(frozen=True)
class StrategyLabSnapshot:
    available: bool
    entries: tuple[dict[str, object], ...] = ()
    instrument_summary: dict[str, object] | None = None
    strategy_summary: dict[str, object] | None = None


def load_strategy_lab_snapshot(
    artifact_directory: Path = DEFAULT_ARTIFACT_DIRECTORY,
) -> StrategyLabSnapshot:
    """Read local generated evidence only; absence remains visible and safe."""

    leaderboard = _load_object(artifact_directory / "leaderboard.json")
    if leaderboard is None or not isinstance(leaderboard.get("entries"), list):
        return StrategyLabSnapshot(available=False)
    entries = tuple(item for item in leaderboard["entries"] if _safe_entry(item))
    return StrategyLabSnapshot(
        available=True,
        entries=entries,
        instrument_summary=_load_object(artifact_directory / "instrument_summary.json"),
        strategy_summary=_load_object(artifact_directory / "strategy_summary.json"),
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
        "version",
        "timeframe",
        "trades",
        "status",
    }
    primitive = str | int | float | type(None)
    return required.issubset(value) and all(
        isinstance(value[field], primitive) for field in required
    )
