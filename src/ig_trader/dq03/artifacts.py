"""Sanitized local DQ-03 evidence artifacts; none grants execution authority."""

# ruff: noqa: E501

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.ig_trader.dq03.acquisition import BrokerValidationSample
from src.ig_trader.dq03.models import DQ03Resolution, DQ03Status, RequestCounters
from src.ig_trader.dq03.strategy_lab import build_strategy_lab_context

ARTIFACT_SCHEMA = "dq03-instrument-resolution/1.0"
RESOLVER_VERSION = "dq03-resolver/1.1"


def write_dq03_artifacts(
    output_directory: Path,
    resolutions: tuple[DQ03Resolution, ...],
    counters: RequestCounters,
    *,
    history_samples: tuple[BrokerValidationSample, ...] = (),
    streaming_result: dict[str, object] | None = None,
    phase: str = "PHASE_1",
    run_context: dict[str, object] | None = None,
) -> dict[str, Path]:
    """Write additive DQ-03 evidence without degrading earlier phase provenance."""

    output_directory.mkdir(parents=True, exist_ok=True)
    registry_path = output_directory / "instrument_registry.json"
    candidate_path = output_directory / "candidate_evidence.json"
    metadata_path = output_directory / "metadata_summary.json"
    manifest_path = output_directory / "discovery_manifest.json"
    history_path = output_directory / "history_validation.json"
    previous_registry = _read_document(registry_path)
    previous_manifest = _read_document(manifest_path)
    phase_one = phase == "PHASE_1"
    if phase_one:
        registry = _phase_one_registry(resolutions, run_context)
        candidate_evidence = _candidate_evidence(resolutions)
        metadata_summary = _metadata_summary(resolutions)
    else:
        registry = _augment_registry(
            previous_registry, resolutions, history_samples, phase, run_context
        )
        candidate_evidence = None
        metadata_summary = None

    registry_path.write_text(_render(registry), encoding="utf-8")
    if candidate_evidence is not None or not candidate_path.exists():
        candidate_path.write_text(
            _render(candidate_evidence or _candidate_evidence(resolutions)), encoding="utf-8"
        )
    if metadata_summary is not None or not metadata_path.exists():
        metadata_path.write_text(
            _render(metadata_summary or _metadata_summary(resolutions)), encoding="utf-8"
        )
    if history_samples:
        history_path.write_text(
            _render(
                {
                    "schema_version": ARTIFACT_SCHEMA,
                    "execution_authority": "OFF",
                    "phase": "PHASE_2",
                    "samples": [sample.document() for sample in history_samples],
                }
            ),
            encoding="utf-8",
        )

    manifest = _manifest(
        resolutions,
        counters,
        phase=phase,
        run_context=run_context,
        streaming_result=streaming_result,
        previous_manifest=previous_manifest,
        history_path=history_path,
    )
    manifest_path.write_text(_render(manifest), encoding="utf-8")
    candidate_registry = output_directory / "candidate_demo_execution_registry.json"
    candidate_registry.write_text(
        _render(candidate_demo_execution_registry(resolutions)), encoding="utf-8"
    )
    paths = {
        registry_path.name: registry_path,
        candidate_path.name: candidate_path,
        metadata_path.name: metadata_path,
        manifest_path.name: manifest_path,
        candidate_registry.name: candidate_registry,
    }
    if history_samples or history_path.exists():
        paths[history_path.name] = history_path
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


def _phase_one_registry(
    resolutions: tuple[DQ03Resolution, ...], run_context: dict[str, object] | None
) -> dict[str, object]:
    return {
        "schema_version": ARTIFACT_SCHEMA,
        "execution_authority": "OFF",
        "phase": "PHASE_1",
        "latest_augmentation_phase": None,
        "run_context": run_context or {},
        "instruments": [item.document() for item in resolutions],
        "strategy_lab_context": build_strategy_lab_context(resolutions),
    }


def _candidate_evidence(resolutions: tuple[DQ03Resolution, ...]) -> dict[str, object]:
    return {
        "schema_version": ARTIFACT_SCHEMA,
        "execution_authority": "OFF",
        "phase": "PHASE_1",
        "instruments": [
            {
                "symbol": item.symbol,
                "classification": item.classification.value,
                "candidates": [candidate.document() for candidate in item.candidates],
            }
            for item in resolutions
        ],
    }


def _metadata_summary(resolutions: tuple[DQ03Resolution, ...]) -> dict[str, object]:
    return {
        "schema_version": ARTIFACT_SCHEMA,
        "execution_authority": "OFF",
        "phase": "PHASE_1",
        "instruments": [_metadata_row(item) for item in resolutions],
    }


def _augment_registry(
    previous: dict[str, object] | None,
    resolutions: tuple[DQ03Resolution, ...],
    samples: tuple[BrokerValidationSample, ...],
    phase: str,
    run_context: dict[str, object] | None,
) -> dict[str, object]:
    """Change only validation fields; keep the original candidate evidence verbatim."""

    prior_instruments = previous.get("instruments") if isinstance(previous, dict) else None
    if not isinstance(prior_instruments, list):
        prior_instruments = []
    prior_by_symbol = {
        value.get("canonical_symbol"): dict(value)
        for value in prior_instruments
        if isinstance(value, dict) and isinstance(value.get("canonical_symbol"), str)
    }
    samples_by_symbol = {sample.symbol: sample for sample in samples}
    instruments: list[dict[str, object]] = []
    for item in resolutions:
        existing = prior_by_symbol.get(item.symbol)
        if existing is None:
            existing = item.document()
        else:
            existing["data_status"] = item.data_status.value
            existing["broker_validation_fingerprint"] = item.broker_validation_fingerprint
            existing["cost_model_status"] = item.cost_model_status.value
        sample = samples_by_symbol.get(item.symbol)
        if sample is not None:
            existing["broker_validation"] = sample.document()
        instruments.append(existing)
    return {
        "schema_version": str(previous.get("schema_version", ARTIFACT_SCHEMA))
        if isinstance(previous, dict)
        else ARTIFACT_SCHEMA,
        "execution_authority": "OFF",
        "phase": "PHASE_1",
        "latest_augmentation_phase": phase,
        "run_context": (previous.get("run_context") if isinstance(previous, dict) else None)
        or run_context
        or {},
        "instruments": instruments,
        "strategy_lab_context": build_strategy_lab_context(resolutions),
    }


def _manifest(
    resolutions: tuple[DQ03Resolution, ...],
    counters: RequestCounters,
    *,
    phase: str,
    run_context: dict[str, object] | None,
    streaming_result: dict[str, object] | None,
    previous_manifest: dict[str, object] | None,
    history_path: Path,
) -> dict[str, object]:
    prior_streaming = (
        previous_manifest.get("streaming_smoke_test")
        if isinstance(previous_manifest, dict)
        else None
    )
    return {
        "schema_version": ARTIFACT_SCHEMA,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "phase": phase,
        "run_context": run_context or {},
        "instrument_count": len(resolutions),
        "classification_counts": {
            status.value: sum(item.classification is status for item in resolutions)
            for status in DQ03Status
        },
        "request_counts": counters.document(),
        "history_validation_artifact_present": history_path.is_file(),
        "streaming_smoke_test": streaming_result
        or prior_streaming
        or {"status": "NOT_RUN", "reason": "Read-only streaming smoke test was not requested."},
        "demo_create_calls": 0,
        "demo_close_calls": 0,
        "execution_authority": "OFF",
    }


def _metadata_row(resolution: DQ03Resolution) -> dict[str, object]:
    metadata = resolution.metadata
    return {
        "symbol": resolution.symbol,
        "classification": resolution.classification.value,
        "selected_epic": resolution.selected_epic,
        "selected_candidate_epic": resolution.selected_epic,
        "selected_candidate_name": resolution.display_name if resolution.selected_epic else None,
        "candidate_score": resolution.selection_score,
        "missing_fields": list(resolution.missing_fields),
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


def _read_document(path: Path) -> dict[str, object] | None:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _render(document: dict[str, object]) -> str:
    return json.dumps(document, sort_keys=True, indent=2, default=str) + "\n"


def phase_context_matches(output_directory: Path, expected: dict[str, object]) -> bool:
    """Allow later read-only phases only for the same fresh Demo resolution evidence."""

    path = output_directory / "discovery_manifest.json"
    document = _read_document(path)
    if document is None:
        return False
    generated = document.get("generated_at_utc")
    freshness_hours = expected.get("metadata_freshness_hours")
    if not isinstance(generated, str) or not isinstance(freshness_hours, int):
        return False
    try:
        generated_at = datetime.fromisoformat(generated.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return False
    fresh = datetime.now(UTC) - generated_at <= timedelta(hours=freshness_hours)
    return fresh and (
        document.get("phase") in {"PHASE_1", "PHASE_2", "PHASE_3"}
        and document.get("execution_authority") == "OFF"
        and document.get("run_context") == expected
    )
