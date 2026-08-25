"""SL-04 offline local-data replay conductor.

SL-04 deliberately consumes already downloaded local research evidence. It does
not instantiate an HTTP client and cannot acquire data, query IG, or send an
order while running a replay.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any

from src.ig_trader.sl02.evidence import preflight_dq03_evidence
from src.ig_trader.sl02.runner import SL02_VERIFIED_SYMBOLS
from src.ig_trader.sl03.runner import SL03BrokerEvidenceRequired, SL03Runner
from src.ig_trader.strategy_lab.segments import GapSafeResearchSegmenter

from .history import SL04SourcePriority
from .local_csv import LocalDukascopyGoCsvSource

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARTIFACT_DIRECTORY = ROOT / "artifacts" / "strategy_lab"
DEFAULT_LOCAL_DATA_DIRECTORY = DEFAULT_ARTIFACT_DIRECTORY / "manual_data" / "dukascopy"
SL04_MANIFEST_NAME = "sl04_source_manifest.json"

# This is the frozen local SL-04 baseline supplied in the remediation work
# order. It is retained only to make the required semantic before/after report
# comparable to the prior all-or-nothing local replay, not as a qualification
# input or a substitute for generated research evidence.
PRE_SEGMENTATION_LOCAL_BASELINE: dict[str, object] = {
    "combinations_scheduled": 300,
    "combinations_simulated": 5,
    "failure_counts": {
        "DATA_QUALITY_FAIL": 267,
        "LOW_DATA_DEPTH": 10,
        "SOURCE_DIVERGENCE": 18,
        "NEGATIVE_EXPECTANCY": 3,
        "OVERFIT_RISK": 2,
    },
    "results": [],
}


@dataclass(frozen=True)
class SL04Run:
    artifact_paths: dict[str, Path]
    combinations_scheduled: int
    combinations_simulated: int
    dataset_count: int
    runtime_seconds: float
    local_files_accepted: int
    local_files_rejected: int


class SL04Runner:
    """Run the frozen SL-03 competition against offline local source priority."""

    def __init__(
        self,
        *,
        artifact_directory: Path,
        dq03_directory: Path,
        yahoo_cache_directory: Path,
        local_data_directory: Path = DEFAULT_LOCAL_DATA_DIRECTORY,
        export_directory: Path | None = None,
        previous_artifact_directory: Path | None = None,
        local_source: LocalDukascopyGoCsvSource | None = None,
    ) -> None:
        self.artifact_directory = artifact_directory
        self.dq03_directory = dq03_directory
        self.yahoo_cache_directory = yahoo_cache_directory
        self.local_data_directory = local_data_directory
        self.export_directory = export_directory or local_data_directory / "reviewed_exports"
        self.previous_artifact_directory = previous_artifact_directory
        self.local_source = local_source or LocalDukascopyGoCsvSource(local_data_directory)

    def run(self) -> SL04Run:
        """Validate local evidence and run the unchanged, research-only SL-03 replay."""

        started = monotonic()
        dq03 = preflight_dq03_evidence(self.dq03_directory, expected_symbols=SL02_VERIFIED_SYMBOLS)
        if not dq03.broker_ready:
            raise SL03BrokerEvidenceRequired("SL03_BROKER_EVIDENCE_REQUIRED")
        self.local_source.prepare()
        source_priority = SL04SourcePriority(
            local_csv=self.local_source,
            export_directory=self.export_directory,
            yahoo_cache_directory=self.yahoo_cache_directory,
        )
        replay = SL03Runner(
            artifact_directory=self.artifact_directory,
            dq03_directory=self.dq03_directory,
            yahoo_cache_directory=self.yahoo_cache_directory,
            history_source=source_priority,
            segmenter=GapSafeResearchSegmenter(),
        ).run()
        source_manifest = self._source_manifest(dq03.document(), replay.runtime_seconds)
        before_after = self._before_after(replay)
        artifact_paths = dict(replay.artifact_paths)
        artifact_paths.update(self._write_sl04_artifacts(source_manifest, before_after))
        validation = self.local_source.import_validation_document()
        return SL04Run(
            artifact_paths=artifact_paths,
            combinations_scheduled=replay.combinations_scheduled,
            combinations_simulated=replay.combinations_simulated,
            dataset_count=replay.dataset_count,
            runtime_seconds=monotonic() - started,
            local_files_accepted=int(validation["files_accepted"]),
            local_files_rejected=int(validation["files_rejected"]),
        )

    def _source_manifest(
        self, dq03_preflight: dict[str, object], replay_runtime_seconds: float
    ) -> dict[str, object]:
        sl03_manifest = _read_object(self.artifact_directory / "sl03_dataset_manifest.json") or {}
        validation = self.local_source.import_validation_document()
        return {
            "schema_version": "strategy-lab-sl04-source-manifest/2.0",
            "phase": "SL04_DEEP_STRUCTURED_HISTORY",
            "mode": "OFFLINE_LOCAL_ONLY",
            "network_acquisition_calls": 0,
            "provider_access": "NOT_USED_LOCAL_CSV_EXPORT",
            "dq03_preflight": dq03_preflight,
            "acquisition": {
                "acquisition_mode": "LOCAL_CSV_EXPORT",
                "network_acquisition_calls": 0,
                "provider_get_count": 0,
                "retries": 0,
                "downloaded_rows": 0,
                "local_rows_imported": validation["raw_rows_imported"],
                "runtime_seconds": replay_runtime_seconds,
            },
            "source_priority": [
                "DUKASCOPY_PUBLIC_FEED_LOCAL_CSV",
                "REVIEWED_LOCAL_DUKASCOPY_EXPORT",
                "YAHOO_CACHED_RESEARCH_SOURCE",
                "DATA_NOT_AVAILABLE",
            ],
            "source_merge_policy": "ONE_COHERENT_PROVIDER_PER_DATASET",
            "provider_spread_policy": (
                "Local provider bid/ask and spread values are data-quality and liquidity "
                "evidence only. SL-03 continues to use the fingerprint-bound DQ-03 "
                "IG-linked friction model; provider spread is never a second execution charge."
            ),
            "datasets": [
                _dataset_facts(item)
                for item in sl03_manifest.get("datasets", [])
                if isinstance(item, dict)
            ],
            "local_import_summary": {
                "files_discovered": validation["files_discovered"],
                "files_accepted": validation["files_accepted"],
                "files_rejected": validation["files_rejected"],
                "raw_rows_imported": validation["raw_rows_imported"],
            },
            "execution_authority": "OFF",
            "safety": _safety(),
        }

    def _before_after(self, replay) -> dict[str, object]:
        previous_sl03 = (
            _read_object(self.previous_artifact_directory / "sl03_results.json")
            if self.previous_artifact_directory
            else None
        )
        after = _read_object(self.artifact_directory / "sl03_results.json") or {}
        return {
            "before": _coverage(PRE_SEGMENTATION_LOCAL_BASELINE),
            "after": _coverage(after, datasets=replay.dataset_count),
            "improvement_in_valid_research_coverage": _coverage_delta(
                PRE_SEGMENTATION_LOCAL_BASELINE, after
            ),
            "before_source": "OPERATOR_REPORTED_PRE_SEGMENTATION_LOCAL_SL04_REPLAY",
            "previous_sl03_source_comparison": _coverage(previous_sl03),
            "comparison_note": (
                "The semantic comparison is the operator-reported all-or-nothing local "
                "SL-04 replay before gap-safe segmentation. Segmentation changes only data "
                "quality handling; SL-03 strategies, grids, entry thresholds, stops, "
                "reward:risk, qualification, walk-forward, bootstrap, stress, and DQ-03 "
                "IG-linked friction remain unchanged."
            ),
            "execution_authority": "OFF",
            "safety": _safety(),
        }

    def _write_sl04_artifacts(
        self, source_manifest: dict[str, object], before_after: dict[str, object]
    ) -> dict[str, Path]:
        sl03 = {
            name: _read_object(self.artifact_directory / name) or {}
            for name in (
                "sl03_data_quality_audit.json",
                "sl03_dataset_manifest.json",
                "sl03_results.json",
                "sl03_signal_funnel.json",
                "sl03_leaderboard.json",
                "sl03_walk_forward.json",
                "sl03_stress_tests.json",
                "sl03_robustness.json",
                "sl03_portfolio.json",
                "sl03_demo_watchlist.json",
                "sl03_demo_candidate_registry.json",
            )
        }
        segmentation = (
            sl03["sl03_dataset_manifest.json"].get("gap_safe_segmentation")
            if isinstance(sl03["sl03_dataset_manifest.json"], dict)
            else None
        )
        if not isinstance(segmentation, dict):
            segmentation = {}
        validation = self.local_source.import_validation_document()
        resampling = self.local_source.resampling_document()
        documents: dict[str, dict[str, object]] = {
            "sl04_local_source_manifest.json": self.local_source.local_source_manifest(),
            SL04_MANIFEST_NAME: source_manifest,
            "sl04_import_validation.json": validation,
            "sl04_resampling_manifest.json": resampling,
            "sl04_segment_manifest.json": _segment_manifest(segmentation),
            "sl04_data_quality_audit.json": _quality_document(
                sl03["sl03_data_quality_audit.json"], validation, resampling, segmentation
            ),
            "sl04_alignment.json": _alignment_document(sl03["sl03_dataset_manifest.json"]),
            "sl04_results.json": _results_document(sl03["sl03_results.json"]),
            "sl04_signal_funnel.json": _signal_funnel_document(
                sl03["sl03_signal_funnel.json"], sl03["sl03_results.json"]
            ),
            "sl04_leaderboard.json": _leaderboard_document(sl03["sl03_leaderboard.json"]),
            "sl04_walk_forward.json": _phase_document(
                sl03["sl03_walk_forward.json"], "walk forward"
            ),
            "sl04_stress_tests.json": _phase_document(
                sl03["sl03_stress_tests.json"], "stress tests"
            ),
            "sl04_robustness.json": _phase_document(sl03["sl03_robustness.json"], "robustness"),
            "sl04_portfolio.json": _phase_document(sl03["sl03_portfolio.json"], "portfolio"),
            "sl04_demo_watchlist.json": _phase_document(
                sl03["sl03_demo_watchlist.json"], "demo watchlist"
            ),
            "sl04_demo_candidate_registry.json": _phase_document(
                sl03["sl03_demo_candidate_registry.json"], "candidate registry"
            ),
            "sl04_before_after.json": before_after,
        }
        documents["sl04_replay.json"] = {
            "schema_version": "strategy-lab-sl04-replay/2.0",
            "phase": "SL04_DEEP_STRUCTURED_HISTORY",
            "mode": "OFFLINE_LOCAL_ONLY",
            "network_acquisition_calls": 0,
            "replay_engine": "SL03Runner with opt-in gap-safe data segmentation",
            "strategy_changes": "NONE",
            "source_manifest": SL04_MANIFEST_NAME,
            "before_after": before_after,
            "results": sl03["sl03_results.json"],
            "execution_authority": "OFF",
            "safety": _safety(),
        }
        return {name: self._write_document(name, document) for name, document in documents.items()}

    def _write_document(self, name: str, document: dict[str, object]) -> Path:
        self.artifact_directory.mkdir(parents=True, exist_ok=True)
        path = self.artifact_directory / name
        path.write_text(
            json.dumps(document, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8"
        )
        return path


def _phase_document(source: dict[str, object], artifact_type: str) -> dict[str, object]:
    return {
        "schema_version": "strategy-lab-sl04-artifact/2.0",
        "phase": "SL04_DEEP_STRUCTURED_HISTORY",
        "artifact_type": artifact_type,
        "source": source,
        "execution_authority": "OFF",
        "safety": _safety(),
    }


def _results_document(source: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "strategy-lab-sl04-results/2.0",
        "phase": "SL04_DEEP_STRUCTURED_HISTORY",
        "sl03_schema_version": source.get("schema_version"),
        "combinations_scheduled": source.get("combinations_scheduled", 0),
        "combinations_simulated": source.get("combinations_simulated", 0),
        "parameter_sets_evaluated": source.get("parameter_sets_evaluated", 0),
        "walk_forward_evaluations": source.get("walk_forward_evaluations", 0),
        "failure_counts": source.get("failure_counts", {}),
        "results": source.get("results", []),
        "execution_authority": "OFF",
        "safety": _safety(),
    }


def _signal_funnel_document(
    source: dict[str, object], results: dict[str, object]
) -> dict[str, object]:
    result_rows = results.get("results") if isinstance(results.get("results"), list) else []
    providers = {
        _entry_identity(item): item.get("data_source")
        for item in result_rows
        if isinstance(item, dict)
    }
    evaluations = source.get("evaluations") if isinstance(source.get("evaluations"), list) else []
    enriched = [
        {**item, "provider": providers.get(_entry_identity(item), "DATA_NOT_AVAILABLE")}
        for item in evaluations
        if isinstance(item, dict)
    ]
    by_provider: dict[str, int] = {}
    for item in enriched:
        provider = str(item["provider"])
        by_provider[provider] = by_provider.get(provider, 0) + 1
    aggregate_fields = (
        "candles_evaluated",
        "raw_strategy_signals",
        "signals_rejected_by_regime_filter",
        "signals_rejected_by_session_filter",
        "signals_rejected_by_cost_or_minimum_stop",
        "signals_rejected_by_minimum_stop",
        "signals_rejected_by_cost_or_spread",
        "signals_while_trade_open",
        "entries_taken",
        "completed_trades",
        "trades_terminated_at_segment_end",
        "oos_trades",
    )
    aggregate = {
        field: sum(int(item.get(field, 0)) for item in enriched) for field in aggregate_fields
    }
    return {
        "schema_version": "strategy-lab-sl04-signal-funnel/2.0",
        "phase": "SL04_DEEP_STRUCTURED_HISTORY",
        "evaluations": enriched,
        "evaluation_count_by_provider": dict(sorted(by_provider.items())),
        "aggregate": aggregate,
        "execution_authority": "OFF",
        "safety": _safety(),
    }


def _entry_identity(value: dict[str, object]) -> tuple[str, str, str, str]:
    return (
        str(value.get("instrument", "")),
        str(value.get("strategy", "")),
        str(value.get("strategy_version", "")),
        str(value.get("timeframe", "")),
    )


def _quality_document(
    source: dict[str, object],
    validation: dict[str, object],
    resampling: dict[str, object],
    segmentation: dict[str, object],
) -> dict[str, object]:
    records = resampling.get("records") if isinstance(resampling.get("records"), list) else []
    records = [item for item in records if isinstance(item, dict)]
    incomplete = sum(int(item.get("derived_omitted_bucket_count", 0)) for item in records)
    roots: dict[tuple[str, str], dict[str, object]] = {}
    for record in records:
        parent = str(record.get("parent_dataset_fingerprint", ""))
        source_gaps = record.get("root_source_gaps")
        if not isinstance(source_gaps, list):
            continue
        for gap in source_gaps:
            if isinstance(gap, dict) and isinstance(gap.get("identifier"), str):
                roots[(parent, gap["identifier"])] = gap
    datasets = (
        segmentation.get("datasets") if isinstance(segmentation.get("datasets"), list) else []
    )
    datasets = [item for item in datasets if isinstance(item, dict)]
    return {
        "schema_version": "strategy-lab-sl04-data-quality-audit/2.0",
        "phase": "SL04_DEEP_STRUCTURED_HISTORY",
        "raw_rows": validation.get("raw_rows_imported", 0),
        "raw_validation_counts": validation.get("raw_validation_counts", {}),
        "datasets_audited": source.get("datasets_audited", 0),
        "gaps_examined": source.get("gaps_examined", 0),
        "classification_counts": source.get("classification_counts", {}),
        "root_source_gap_count": len(roots),
        "root_source_missing_intervals": sum(
            int(item.get("missing_intervals", 0)) for item in roots.values()
        ),
        "derived_omitted_bucket_count": incomplete,
        "derived_incomplete_buckets": incomplete,
        "derived_gap_interpretation": (
            "DERIVED_BUCKET_OMITTED rows preserve hard research boundaries but are not "
            "additional independent provider failures; their SOURCE_GAP lineage is in the "
            "resampling manifest."
        ),
        "segmentation_summary": {
            "datasets_segmented": len(datasets),
            "segments_created": sum(int(item.get("segment_count", 0)) for item in datasets),
            "eligible_segments": sum(
                int(item.get("eligible_segment_count", 0)) for item in datasets
            ),
            "short_segments": sum(int(item.get("short_segment_count", 0)) for item in datasets),
            "average_clean_coverage_ratio": (
                sum(Decimal(str(item.get("clean_coverage_ratio", 0))) for item in datasets)
                / Decimal(len(datasets))
                if datasets
                else Decimal("0")
            ),
        },
        "gaps": source.get("gaps", []),
        "policy": source.get("policy"),
        "execution_authority": "OFF",
        "safety": _safety(),
    }


def _segment_manifest(segmentation: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "strategy-lab-sl04-segment-manifest/1.0",
        "phase": "SL04_DEEP_STRUCTURED_HISTORY",
        "segmentation": segmentation,
        "execution_authority": "OFF",
        "safety": _safety(),
    }


def _leaderboard_document(source: dict[str, object]) -> dict[str, object]:
    entries = source.get("entries") if isinstance(source.get("entries"), list) else []
    return {
        "schema_version": "strategy-lab-sl04-leaderboard/2.0",
        "phase": "SL04_DEEP_STRUCTURED_HISTORY",
        "entries": entries,
        "execution_authority": "OFF",
        "safety": _safety(),
    }


def _alignment_document(manifest: dict[str, object]) -> dict[str, object]:
    rows = manifest.get("datasets") if isinstance(manifest.get("datasets"), list) else []
    alignments = [
        {
            "instrument": item.get("instrument"),
            "timeframe": item.get("timeframe"),
            "provider": item.get("provider"),
            "ig_epic": item.get("ig_epic"),
            "alignment": item.get("ig_alignment"),
        }
        for item in rows
        if isinstance(item, dict)
    ]
    counts: dict[str, int] = {}
    for item in alignments:
        alignment = item.get("alignment")
        status = alignment.get("status") if isinstance(alignment, dict) else "NO_ALIGNMENT_RECORD"
        counts[str(status)] = counts.get(str(status), 0) + 1
    return {
        "schema_version": "strategy-lab-sl04-alignment/2.0",
        "phase": "SL04_DEEP_STRUCTURED_HISTORY",
        "classifications": dict(sorted(counts.items())),
        "datasets": alignments,
        "execution_authority": "OFF",
        "safety": _safety(),
    }


def _dataset_facts(row: dict[str, object]) -> dict[str, object]:
    """Add SL-04 audit fields without altering the SL-03 dataset evidence."""

    result = dict(row)
    segmentation = result.get("gap_safe_segmentation")
    if isinstance(segmentation, dict):
        result["quality_status"] = (
            "QUALITY_AUDITED_WITH_HARD_BOUNDARIES"
            if int(segmentation.get("eligible_segment_count", 0)) > 0
            else "DATA_QUALITY_FAIL"
        )
        result["clean_coverage_ratio"] = segmentation.get("clean_coverage_ratio")
        result["usable_clean_coverage_ratio"] = segmentation.get("usable_clean_coverage_ratio")
        result["eligible_segment_count"] = segmentation.get("eligible_segment_count")
    else:
        unexplained = int(result.get("unexplained_gap_count", 0))
        result["quality_status"] = "DATA_QUALITY_FAIL" if unexplained else "QUALITY_AUDITED"
    result["calendar_duration_seconds"] = _duration_seconds(result)
    if result.get("provider") == "DUKASCOPY_PUBLIC_FEED":
        result["bid_ask_availability"] = "AVAILABLE_PROVIDER_EVIDENCE"
    return result


def _duration_seconds(row: dict[str, object]) -> float | None:
    date_range = row.get("date_range")
    if not isinstance(date_range, dict):
        return None
    first, last = date_range.get("first_utc"), date_range.get("last_utc")
    if not isinstance(first, str) or not isinstance(last, str):
        return None
    try:
        return (
            datetime.fromisoformat(last.replace("Z", "+00:00"))
            - datetime.fromisoformat(first.replace("Z", "+00:00"))
        ).total_seconds()
    except ValueError:
        return None


def _read_object(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _coverage(
    document: dict[str, object] | None, *, datasets: int | None = None
) -> dict[str, object]:
    if document is None:
        return {"status": "PREVIOUS_SL03_ARTIFACT_NOT_AVAILABLE"}
    scheduled = int(document.get("combinations_scheduled", 0))
    simulated = int(document.get("combinations_simulated", 0))
    percentage = Decimal(simulated) / Decimal(scheduled) * 100 if scheduled else Decimal("0")
    results = document.get("results") if isinstance(document.get("results"), list) else []
    return {
        "status": "AVAILABLE",
        "datasets": datasets,
        "scheduled_combinations": scheduled,
        "simulated_combinations": simulated,
        "simulation_percentage": percentage,
        "block_counts": document.get("failure_counts", {}),
        "ready_for_demo": sum(
            item.get("classification") == "READY_FOR_DEMO_QUALIFICATION"
            for item in results
            if isinstance(item, dict)
        ),
    }


def _coverage_delta(
    before: dict[str, object] | None, after: dict[str, object]
) -> dict[str, object]:
    before_simulated = int(before.get("combinations_simulated", 0)) if before else 0
    after_simulated = int(after.get("combinations_simulated", 0))
    return {
        "additional_simulated_combinations": after_simulated - before_simulated,
        "before_simulated_combinations": before_simulated,
        "after_simulated_combinations": after_simulated,
    }


def _safety() -> dict[str, object]:
    return {
        "network_acquisition_calls": 0,
        "ig_create_calls": 0,
        "ig_close_calls": 0,
        "live_calls": 0,
        "azure_calls": 0,
        "execution_authority": "OFF",
        "order_endpoints": "NOT_PRESENT",
    }
