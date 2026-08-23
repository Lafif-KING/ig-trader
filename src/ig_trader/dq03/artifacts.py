"""Sanitized local DQ-03 evidence artifacts; none grants execution authority."""

# ruff: noqa: E501

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from src.ig_trader.dq03.models import DQ03Resolution, DQ03Status, RequestCounters
from src.ig_trader.dq03.strategy_lab import build_strategy_lab_context

ARTIFACT_SCHEMA = "dq03-instrument-resolution/1.0"


def write_dq03_artifacts(
    output_directory: Path,
    resolutions: tuple[DQ03Resolution, ...],
    counters: RequestCounters,
    *,
    streaming_result: dict[str, object] | None = None,
) -> dict[str, Path]:
    """Write the four required ignored runtime artifacts in a deterministic shape."""

    output_directory.mkdir(parents=True, exist_ok=True)
    documents = {
        "instrument_registry.json": {
            "schema_version": ARTIFACT_SCHEMA,
            "execution_authority": "OFF",
            "instruments": [item.document() for item in resolutions],
            "strategy_lab_context": build_strategy_lab_context(resolutions),
        },
        "candidate_evidence.json": {
            "schema_version": ARTIFACT_SCHEMA,
            "execution_authority": "OFF",
            "instruments": [
                {
                    "symbol": item.symbol,
                    "classification": item.classification.value,
                    "candidates": [candidate.document() for candidate in item.candidates],
                }
                for item in resolutions
            ],
        },
        "metadata_summary.json": {
            "schema_version": ARTIFACT_SCHEMA,
            "execution_authority": "OFF",
            "instruments": [_metadata_row(item) for item in resolutions],
        },
        "discovery_manifest.json": {
            "schema_version": ARTIFACT_SCHEMA,
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "instrument_count": len(resolutions),
            "classification_counts": {
                status.value: sum(item.classification is status for item in resolutions)
                for status in DQ03Status
            },
            "request_counts": counters.document(),
            "streaming_smoke_test": streaming_result
            or {"status": "NOT_RUN", "reason": "Read-only streaming smoke test was not requested."},
            "demo_create_calls": 0,
            "demo_close_calls": 0,
            "execution_authority": "OFF",
        },
    }
    paths: dict[str, Path] = {}
    for filename, document in documents.items():
        path = output_directory / filename
        path.write_text(_render(document), encoding="utf-8")
        paths[filename] = path
    candidate_registry = output_directory / "candidate_demo_execution_registry.json"
    candidate_registry.write_text(
        _render(candidate_demo_execution_registry(resolutions)), encoding="utf-8"
    )
    paths[candidate_registry.name] = candidate_registry
    return paths


def candidate_demo_execution_registry(
    resolutions: tuple[DQ03Resolution, ...],
) -> dict[str, object]:
    """Report possible future review inputs without creating a DQ-02 registration."""

    candidates = [
        {
            "symbol": item.symbol,
            "epic": item.selected_epic,
            "metadata_fingerprint": item.metadata_fingerprint,
            "broker_validation_fingerprint": item.broker_validation_fingerprint,
            "reason": "No READY_FOR_DEMO_QUALIFICATION Strategy Lab evidence exists.",
        }
        for item in resolutions
        if item.classification is DQ03Status.VERIFIED
        and item.data_status.value == "BROKER_VALIDATED"
        and item.cost_model_status.value != "COST_MODEL_INCOMPLETE"
    ]
    return {
        "schema_version": "dq03-candidate-demo-registry/1.0",
        "execution_authority": "OFF",
        "activation_required": "Separate reviewed DQ-02 activation step required.",
        "registrations": candidates,
    }


def _metadata_row(resolution: DQ03Resolution) -> dict[str, object]:
    metadata = resolution.metadata
    return {
        "symbol": resolution.symbol,
        "classification": resolution.classification.value,
        "selected_epic": resolution.selected_epic,
        "display_name": resolution.display_name,
        "currency": metadata.currency if metadata else None,
        "minimum_deal_size": str(metadata.minimum_deal_size) if metadata else None,
        "minimum_stop_distance": str(metadata.minimum_stop_distance) if metadata else None,
        "market_status": metadata.market_status if metadata else None,
        "streaming_prices_available": metadata.streaming_prices_available if metadata else None,
        "spread": str(metadata.spread) if metadata and metadata.spread is not None else None,
        "data_status": resolution.data_status.value,
        "cost_model_status": resolution.cost_model_status.value,
        "metadata_fingerprint": resolution.metadata_fingerprint,
    }


def _render(document: dict[str, object]) -> str:
    return json.dumps(document, sort_keys=True, indent=2, default=str) + "\n"
