"""Ignored, machine-readable SL-02 research artifact generation."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

ARTIFACT_SCHEMA = "strategy-lab-sl02/1.0"


def write_sl02_artifacts(output_directory: Path, documents: dict[str, object]) -> dict[str, Path]:
    """Write the SL-02 evidence set without raw datasets or execution authority."""

    output_directory.mkdir(parents=True, exist_ok=True)
    required = {
        "sl02_dataset_manifest.json",
        "sl02_results.json",
        "sl02_leaderboard.json",
        "sl02_walk_forward.json",
        "sl02_stress_tests.json",
        "sl02_portfolio.json",
        "demo_candidate_registry.json",
    }
    if set(documents) != required:
        raise ValueError("SL-02 artifact set must be complete and exact")
    paths: dict[str, Path] = {}
    for name, document in documents.items():
        path = output_directory / name
        envelope = {
            "schema_version": ARTIFACT_SCHEMA,
            "execution_authority": "OFF",
            **(document if isinstance(document, dict) else {"document": document}),
        }
        path.write_text(
            json.dumps(envelope, sort_keys=True, indent=2, default=_json_default) + "\n",
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
