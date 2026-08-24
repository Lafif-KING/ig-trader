"""Ignored, machine-readable SL-03 research artifact writer."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

ARTIFACT_SCHEMA = "strategy-lab-sl03/1.0"
REQUIRED_ARTIFACTS = frozenset(
    {
        "sl03_data_quality_audit.json",
        "sl03_dataset_manifest.json",
        "sl03_signal_funnel.json",
        "sl03_results.json",
        "sl03_leaderboard.json",
        "sl03_walk_forward.json",
        "sl03_stress_tests.json",
        "sl03_robustness.json",
        "sl03_portfolio.json",
        "sl03_demo_watchlist.json",
        "sl03_demo_candidate_registry.json",
    }
)


def write_sl03_artifacts(output_directory: Path, documents: dict[str, object]) -> dict[str, Path]:
    if set(documents) != REQUIRED_ARTIFACTS:
        raise ValueError("SL-03 artifact set must be complete and exact")
    output_directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, document in documents.items():
        path = output_directory / name
        path.write_text(
            json.dumps(
                {
                    "schema_version": ARTIFACT_SCHEMA,
                    "execution_authority": "OFF",
                    **(document if isinstance(document, dict) else {"document": document}),
                },
                sort_keys=True,
                indent=2,
                default=_json_default,
            )
            + "\n",
            encoding="utf-8",
        )
        paths[name] = path
    return paths


def _json_default(value: Any) -> object:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, frozenset):
        return sorted(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")
