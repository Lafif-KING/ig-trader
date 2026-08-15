"""G3A-02 targeted, GET-only recovery of the EUR/GBP 19:03 UTC minute."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.ig_trader.g3a_data import (
    EMPTY_FINDINGS,
    SECOND_AGGREGATION_VERSION,
    CanonicalCandle,
    FinalClassification,
    RawFileEvidence,
    aggregate_second_candles,
    canonical_candle_document,
    canonical_json_bytes,
    compare_bid_offer_fields,
    normalize_candle,
    sha256_bytes,
    utc_text,
)
from tools.g3a_market_data import (
    FROZEN_INSTRUMENTS,
    G3AConfig,
    G3AError,
    G3APipeline,
    SafeG3ARestClient,
    load_config,
    parse_price_page,
    scan_for_secrets,
    write_create_or_verify,
    write_json_create_only,
)

WORK_ORDER = "G3A-02"
TARGET_EPIC = "CS.D.EURGBP.MINI.IP"
WINDOW_START = datetime(2026, 8, 14, 19, 2, tzinfo=UTC)
TARGET_MINUTE = datetime(2026, 8, 14, 19, 3, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 14, 19, 5, tzinfo=UTC)
MAXIMUM_RECOVERY_PAGES = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="G3A-02 fresh native-minute and conditional SECOND recovery"
    )
    parser.add_argument("--environment", required=True, choices=("demo",))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--dotenv", type=Path, default=Path(".env"))
    return parser


def load_recovery_config(args: argparse.Namespace) -> G3AConfig:
    namespace = argparse.Namespace(
        dotenv=args.dotenv,
        data_root=args.output_root / "data",
        evidence_json=args.output_root / "evidence" / "g3a-02-gap-recovery.json",
        run_id=args.run_id,
        end_utc=WINDOW_END,
        intervals_per_series=60,
        raw_cache_run_id=None,
        offline_cache_only=False,
        source_acquisition_evidence=None,
    )
    return load_config(namespace)


def run_recovery(config: G3AConfig) -> dict[str, object]:
    """Execute one fresh Demo session with no dealing endpoint in the allow-list."""

    raw_run = config.data_root / "raw" / config.run_id
    if raw_run.exists() or config.evidence_json.exists() or config.evidence_markdown.exists():
        raise G3AError(FinalClassification.INCONCLUSIVE, "FRESH_RECOVERY_OUTPUT_REQUIRED")
    rest = SafeG3ARestClient(config)
    pipeline = G3APipeline(config, rest=rest)
    recovered: CanonicalCandle | None = None
    outcome = "GAP_RETAINED"
    failure_reason: str | None = None
    native_result: dict[str, object] = {}
    second_result: dict[str, object] = {
        "attempted": False,
        "availability": "NOT_TESTED_NATIVE_RECOVERY_SUCCEEDED",
    }
    reconstruction: dict[str, object] = {
        "attempted": False,
        "algorithm_version": SECOND_AGGREGATION_VERSION,
    }
    decimal_places: int | None = None
    try:
        pipeline.tokens = pipeline._login()
        timezone_offset = pipeline._account_timezone_offset()
        instrument = next(item for item in FROZEN_INSTRUMENTS if item.epic == TARGET_EPIC)
        verification = pipeline._verify_instrument(instrument)
        pipeline.epic_results = (verification,)
        if not verification.verified:
            raise G3AError(FinalClassification.SCHEMA_GAP, "EPIC_VERIFICATION_FAILED")
        detail = pipeline.raw.load(f"market-{instrument.symbol}.json")
        if detail is None:
            raise G3AError(FinalClassification.SCHEMA_GAP, "MARKET_DETAIL_RAW_MISSING")
        detail_payload, _detail_file = detail
        decimal_places = _decimal_places(detail_payload)

        native_candles, native_files, native_metadata = _fetch_and_normalize(
            pipeline,
            resolution="MINUTE",
            query_start=WINDOW_START,
            query_end_inclusive=WINDOW_END - timedelta(minutes=1),
            canonical_end_exclusive=WINDOW_END,
            timezone_offset=timezone_offset,
            page_size=10,
        )
        native_by_time = {candle.timestamp_utc: candle for candle in native_candles}
        native_result = {
            "request_start_utc": utc_text(WINDOW_START),
            "request_end_inclusive_utc": utc_text(WINDOW_END - timedelta(minutes=1)),
            "returned_candle_count": len(native_candles),
            "returned_timestamps_utc": [
                utc_text(candle.timestamp_utc) for candle in native_candles
            ],
            "target_returned": TARGET_MINUTE in native_by_time,
            "source_files": [_raw_file_value(item) for item in native_files],
            "allowance_remaining": _remaining_allowance(native_metadata),
        }
        if TARGET_MINUTE in native_by_time:
            recovered = replace(
                native_by_time[TARGET_MINUTE],
                provenance="IG_NATIVE_1M",
                aggregation_version=None,
                source_component_count=1,
            )
            outcome = "RECOVERED_NATIVE_1M"
        else:
            second_result["attempted"] = True
            try:
                second_candles, second_files, second_metadata = _fetch_and_normalize(
                    pipeline,
                    resolution="SECOND",
                    query_start=WINDOW_START,
                    query_end_inclusive=WINDOW_END - timedelta(seconds=1),
                    canonical_end_exclusive=WINDOW_END,
                    timezone_offset=timezone_offset,
                    page_size=180,
                )
            except G3AError as error:
                second_result.update(
                    {
                        "availability": "UNAVAILABLE_OR_REJECTED",
                        "reason": error.reason,
                        "source_files": _raw_files_for_resolution(
                            pipeline.raw_files.values(), "SECOND"
                        ),
                    }
                )
                second_candles = []
                second_files = ()
                second_metadata = ()
            else:
                second_result.update(
                    {
                        "availability": ("AVAILABLE" if second_candles else "SUPPORTED_BUT_EMPTY"),
                        "returned_candle_count": len(second_candles),
                        "returned_timestamps_start_utc": (
                            utc_text(min(item.timestamp_utc for item in second_candles))
                            if second_candles
                            else None
                        ),
                        "returned_timestamps_end_utc": (
                            utc_text(max(item.timestamp_utc for item in second_candles))
                            if second_candles
                            else None
                        ),
                        "source_files": [_raw_file_value(item) for item in second_files],
                        "allowance_remaining": _remaining_allowance(second_metadata),
                    }
                )

            if second_candles:
                source_raw_file = "|".join(item.relative_path for item in second_files)
                reconstructed = {
                    minute: aggregate_second_candles(
                        second_candles,
                        minute,
                        epic=TARGET_EPIC,
                        source_raw_file=source_raw_file,
                        require_every_second=False,
                    )
                    for minute in (
                        WINDOW_START,
                        TARGET_MINUTE,
                        TARGET_MINUTE + timedelta(minutes=1),
                    )
                }
                adjacent_comparisons: dict[str, object] = {}
                for minute in (WINDOW_START, TARGET_MINUTE + timedelta(minutes=1)):
                    candidate = reconstructed[minute]
                    native = native_by_time.get(minute)
                    rows = (
                        compare_bid_offer_fields(
                            candidate,
                            native,
                            decimal_places=decimal_places,
                        )
                        if candidate is not None and native is not None
                        else ()
                    )
                    adjacent_comparisons[utc_text(minute) or ""] = {
                        "native_present": native is not None,
                        "reconstructed_complete": candidate is not None,
                        "fields": list(rows),
                        "all_fields_match": bool(rows) and all(bool(row["match"]) for row in rows),
                    }
                reconstruction = {
                    "attempted": True,
                    "algorithm_version": SECOND_AGGREGATION_VERSION,
                    "rule": (
                        "Use only returned broker second-bars; open=first returned second open; "
                        "high=max returned second highs; low=min returned second lows; "
                        "close=last returned second close. No absent second is filled."
                    ),
                    "missing_seconds_filled": 0,
                    "minutes": {
                        utc_text(minute) or "": _reconstruction_value(candidate)
                        for minute, candidate in reconstructed.items()
                    },
                    "adjacent_native_comparison": adjacent_comparisons,
                    "broker_decimal_places": decimal_places,
                    "validation_succeeded": all(
                        item["all_fields_match"] is True
                        for item in adjacent_comparisons.values()
                        if isinstance(item, Mapping)
                    )
                    and len(adjacent_comparisons) == 2,
                }
                if (
                    reconstruction["validation_succeeded"] is True
                    and reconstructed[TARGET_MINUTE] is not None
                ):
                    recovered = reconstructed[TARGET_MINUTE]
                    outcome = "RECOVERED_DERIVED_FROM_IG_SECOND"
    except G3AError as error:
        failure_reason = error.reason
    finally:
        pipeline._logout_safely()
        rest.close()

    recovery_file: Path | None = None
    recovered_hash: str | None = None
    if recovered is not None:
        recovery_file = config.evidence_json.parent / "recovered-19-03-candle.json"
        recovered_document = canonical_candle_document(recovered)
        recovered_hash = sha256_bytes(canonical_json_bytes(recovered_document))
        write_json_create_only(recovery_file, recovered_document)
    evidence: dict[str, object] = {
        "schema_version": "g3a-gap-recovery-evidence/1.0.0",
        "work_order": WORK_ORDER,
        "environment": "DEMO",
        "run_id": config.run_id,
        "target_epic": TARGET_EPIC,
        "target_interval_start_utc": utc_text(TARGET_MINUTE),
        "target_interval_end_utc": "2026-08-14T19:03:59.999Z",
        "fresh_request_required": True,
        "existing_cache_used": False,
        "native_1m": native_result,
        "second_resolution": second_result,
        "reconstruction": reconstruction,
        "outcome": outcome,
        "failure_reason": failure_reason,
        "recovered_candle": {
            "relative_path": (
                recovery_file.relative_to(config.evidence_json.parent.parent).as_posix()
                if recovery_file is not None
                else None
            ),
            "sha256": recovered_hash,
            "provenance": recovered.provenance if recovered is not None else None,
        },
        "safety": {
            "environment_demo": True,
            "order_authority": "NONE",
            "order_endpoint_call_count": rest.order_endpoint_call_count,
            "blocked_endpoint_attempt_count": rest.blocked_endpoint_attempt_count,
            "market_data_get_call_count": rest.market_data_get_call_count,
            "authentication_endpoint_call_count": rest.authentication_endpoint_call_count,
            "execution_adapter_initialized": False,
            "position_creation_endpoint_called": False,
            "working_order_endpoint_called": False,
            "optimization_run": False,
            "session_cleanup": pipeline.session_cleanup,
        },
        "request_history": rest.request_history,
        "raw_files": [
            _raw_file_value(item)
            for item in sorted(pipeline.raw_files.values(), key=lambda item: item.relative_path)
        ],
        "secret_scan": {"status": "NOT_RUN"},
    }
    artifact_paths = [config.data_root / item.relative_path for item in pipeline.raw_files.values()]
    if recovery_file is not None:
        artifact_paths.append(recovery_file)
    evidence["secret_scan"] = scan_for_secrets(
        artifact_paths,
        config.secret_values(),
        prospective_evidence=evidence,
    )
    if evidence["secret_scan"]["status"] != "PASS":  # type: ignore[index]
        raise G3AError(FinalClassification.DATA_QUALITY_FAILURE, "SECRET_SCAN_FAILED")
    write_json_create_only(config.evidence_json, evidence)
    write_create_or_verify(
        config.evidence_markdown,
        render_markdown(evidence).encode("utf-8"),
    )
    return evidence


def _fetch_and_normalize(
    pipeline: G3APipeline,
    *,
    resolution: str,
    query_start: datetime,
    query_end_inclusive: datetime,
    canonical_end_exclusive: datetime,
    timezone_offset: timedelta,
    page_size: int,
) -> tuple[
    list[CanonicalCandle],
    tuple[RawFileEvidence, ...],
    tuple[Mapping[str, object], ...],
]:
    candles: list[CanonicalCandle] = []
    source_files: list[RawFileEvidence] = []
    metadata_rows: list[Mapping[str, object]] = []
    total_pages: int | None = None
    for page_number in range(1, MAXIMUM_RECOVERY_PAGES + 1):
        raw_name = f"recovery-EURGBP-{resolution}-page-{page_number:03d}.json"
        payload, raw_file = pipeline._request_market_json(
            raw_name,
            f"/prices/{TARGET_EPIC}",
            version="3",
            params={
                "resolution": resolution,
                "from": _request_time(query_start + timezone_offset),
                "to": _request_time(query_end_inclusive + timezone_offset),
                "pageSize": page_size,
                "pageNumber": page_number,
            },
        )
        parsed = parse_price_page(payload)
        if parsed is None:
            raise G3AError(FinalClassification.SCHEMA_GAP, "RECOVERY_PAGE_SCHEMA_INVALID")
        prices, metadata, actual_page, observed_total = parsed
        if actual_page != page_number or observed_total > MAXIMUM_RECOVERY_PAGES:
            raise G3AError(FinalClassification.SCHEMA_GAP, "RECOVERY_PAGINATION_INVALID")
        if total_pages is None:
            total_pages = observed_total
        if observed_total != total_pages:
            raise G3AError(FinalClassification.SCHEMA_GAP, "RECOVERY_PAGINATION_CHANGED")
        source_files.append(raw_file)
        metadata_rows.append(metadata)
        for source_index, raw_candle in enumerate(prices):
            candle, findings = normalize_candle(
                raw_candle,
                epic=TARGET_EPIC,
                resolution=resolution,
                source_page=page_number,
                source_index=source_index,
                source_raw_file=raw_file.relative_path,
                requested_start_utc=query_start,
                requested_end_utc=canonical_end_exclusive,
            )
            if findings != EMPTY_FINDINGS and findings.rejected_candle_count:
                raise G3AError(
                    FinalClassification.DATA_QUALITY_FAILURE,
                    "RECOVERY_CANDLE_REJECTED",
                )
            if candle is not None:
                candles.append(candle)
        if page_number == observed_total:
            break
    else:
        raise G3AError(FinalClassification.SCHEMA_GAP, "RECOVERY_PAGINATION_INCOMPLETE")
    return candles, tuple(source_files), tuple(metadata_rows)


def _decimal_places(payload: Mapping[str, Any]) -> int:
    snapshot = payload.get("snapshot")
    value = snapshot.get("decimalPlacesFactor") if isinstance(snapshot, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 12:
        raise G3AError(FinalClassification.SCHEMA_GAP, "BROKER_PRECISION_UNAVAILABLE")
    return value


def _remaining_allowance(metadata_rows: Sequence[Mapping[str, object]]) -> int | None:
    values = []
    for metadata in metadata_rows:
        allowance = metadata.get("allowance")
        value = allowance.get("remainingAllowance") if isinstance(allowance, Mapping) else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            values.append(value)
    return min(values) if values else None


def _request_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _raw_file_value(value: RawFileEvidence) -> dict[str, object]:
    return {
        "relative_path": value.relative_path,
        "sha256": value.sha256,
        "size_bytes": value.size_bytes,
    }


def _raw_files_for_resolution(
    files: Sequence[RawFileEvidence], resolution: str
) -> list[dict[str, object]]:
    marker = f"-{resolution}-"
    return [
        _raw_file_value(item)
        for item in sorted(files, key=lambda item: item.relative_path)
        if marker in item.relative_path
    ]


def _reconstruction_value(value: CanonicalCandle | None) -> dict[str, object]:
    if value is None:
        return {
            "complete_60_seconds": False,
            "candle": None,
            "sha256": None,
        }
    document = canonical_candle_document(value)
    return {
        "complete_60_seconds": True,
        "candle": document,
        "sha256": sha256_bytes(canonical_json_bytes(document)),
        "source_component_count": value.source_component_count,
        "provenance": value.provenance,
    }


def render_markdown(evidence: Mapping[str, object]) -> str:
    native = evidence.get("native_1m")
    second = evidence.get("second_resolution")
    reconstruction = evidence.get("reconstruction")
    safety = evidence.get("safety")
    lines = [
        "# G3A-02 Targeted Gap Recovery Evidence",
        "",
        f"Outcome: **{evidence.get('outcome')}**",
        "",
        f"Native target returned: `{_get(native, 'target_returned')}`",
        f"SECOND availability: `{_get(second, 'availability')}`",
        f"Reconstruction validation: `{_get(reconstruction, 'validation_succeeded')}`",
        f"Recovered provenance: `{_get(evidence.get('recovered_candle'), 'provenance')}`",
        "",
        "## Safety",
        "",
        f"- Market-data GET calls: `{_get(safety, 'market_data_get_call_count')}`",
        f"- Order endpoint calls: `{_get(safety, 'order_endpoint_call_count')}`",
        f"- Execution adapter initialized: `{_get(safety, 'execution_adapter_initialized')}`",
        "",
    ]
    adjacent = _get(reconstruction, "adjacent_native_comparison")
    if isinstance(adjacent, Mapping):
        for timestamp, comparison in adjacent.items():
            lines.extend(
                [
                    f"## Adjacent comparison {timestamp}",
                    "",
                    "| Field | Reconstructed | Native | Delta | Match |",
                    "|---|---:|---:|---:|---|",
                ]
            )
            fields = _get(comparison, "fields")
            if isinstance(fields, list):
                for row in fields:
                    if isinstance(row, Mapping):
                        lines.append(
                            f"| {row.get('field')} | {row.get('reconstructed')} | "
                            f"{row.get('native')} | {row.get('delta')} | {row.get('match')} |"
                        )
            lines.append("")
    return "\n".join(lines)


def _get(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, Mapping) else None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_recovery_config(args)
        evidence = run_recovery(config)
    except G3AError as error:
        print("G3A_02_GAP_RECOVERY_FAILED", file=sys.stderr)
        print(f"classification={error.classification.value}", file=sys.stderr)
        print(f"reason={error.reason}", file=sys.stderr)
        print("order_endpoint_call_count=0", file=sys.stderr)
        return 2
    print("G3A_02_GAP_RECOVERY_COMPLETE")
    print(f"outcome={evidence['outcome']}")
    print(f"order_endpoint_call_count={evidence['safety']['order_endpoint_call_count']}")  # type: ignore[index]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
