"""Offline G3A-02 analysis of the immutable targeted recovery responses."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.ig_trader.g3a_data import (
    EMPTY_FINDINGS,
    SECOND_AGGREGATION_VERSION,
    CanonicalCandle,
    aggregate_second_candles,
    canonical_candle_document,
    canonical_json_bytes,
    compare_bid_offer_fields,
    fingerprint,
    normalize_candle,
    sha256_bytes,
    utc_text,
)
from tools.g3a_gap_recovery import TARGET_EPIC, TARGET_MINUTE, WINDOW_END, WINDOW_START
from tools.g3a_market_data import (
    safe_raw_payload,
    write_create_or_verify,
    write_json_create_only,
)

POLICY_ID = "GAP_AWARE_REPLAY_V1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline G3A-02 recovery analysis")
    parser.add_argument("--g3a01-evidence", type=Path, required=True)
    parser.add_argument("--recovery-root", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser


def analyze(
    *,
    g3a01_evidence: Path,
    recovery_root: Path,
) -> dict[str, object]:
    original = _json_object(g3a01_evidence)
    recovery_evidence_path = recovery_root / "evidence" / "g3a-02-gap-recovery.json"
    recovery = _json_object(recovery_evidence_path)
    run_id = recovery.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("recovery run id is invalid")
    raw_root = recovery_root / "data" / "raw" / run_id
    native_path = raw_root / "recovery-EURGBP-MINUTE-page-001.json"
    second_path = raw_root / "recovery-EURGBP-SECOND-page-001.json"
    detail_path = raw_root / "market-EURGBP.json"
    native = _normalize_raw(native_path, "MINUTE")
    seconds = _normalize_raw(second_path, "SECOND")
    decimal_places = _decimal_places(_json_object(detail_path))
    native_by_minute = {item.timestamp_utc: item for item in native}
    second_counts = Counter(item.timestamp_utc.replace(second=0) for item in seconds)
    source_hash = sha256_bytes(second_path.read_bytes())
    reconstructed = {
        minute: aggregate_second_candles(
            seconds,
            minute,
            epic=TARGET_EPIC,
            source_raw_file=f"sha256:{source_hash}",
            require_every_second=False,
        )
        for minute in (
            WINDOW_START,
            TARGET_MINUTE,
            TARGET_MINUTE.replace(minute=4),
        )
    }
    adjacent_comparison: dict[str, object] = {}
    for minute in (WINDOW_START, TARGET_MINUTE.replace(minute=4)):
        candidate = reconstructed[minute]
        native_candle = native_by_minute.get(minute)
        rows = (
            compare_bid_offer_fields(
                candidate,
                native_candle,
                decimal_places=decimal_places,
            )
            if candidate is not None and native_candle is not None
            else ()
        )
        adjacent_comparison[utc_text(minute) or ""] = {
            "returned_second_count": second_counts[minute],
            "reconstructed_candle": (
                canonical_candle_document(candidate) if candidate is not None else None
            ),
            "reconstructed_candle_sha256": (
                sha256_bytes(canonical_json_bytes(canonical_candle_document(candidate)))
                if candidate is not None
                else None
            ),
            "native_candle": (
                canonical_candle_document(native_candle) if native_candle is not None else None
            ),
            "fields": list(rows),
            "all_fields_match": bool(rows) and all(bool(row["match"]) for row in rows),
        }
    adjacent_valid = len(adjacent_comparison) == 2 and all(
        isinstance(value, Mapping) and value.get("all_fields_match") is True
        for value in adjacent_comparison.values()
    )
    target_seconds = second_counts[TARGET_MINUTE]
    target_reconstructed = reconstructed[TARGET_MINUTE]
    target_native = native_by_minute.get(TARGET_MINUTE)
    policy = {
        "policy_id": POLICY_ID,
        "version": "1.0.0",
        "trigger": "AUTHORITATIVE_REQUIRED_INTERVAL_MISSING",
        "affected_signal_evaluation": "NO_TRADE",
        "discontinuous_indicator_state": "INVALIDATED",
        "warmup_rebuild": "SUBSEQUENT_AUTHORITATIVE_CANDLES_ONLY",
        "resume_condition": ("EVERY_REQUIRED_TIMEFRAME_AND_INDICATOR_VALID_AFTER_FULL_WARMUP"),
        "gap_event_recording": "REQUIRED_IN_REPLAY_EVIDENCE",
        "interpolation": "PROHIBITED",
        "carry_forward": "PROHIBITED",
        "cross_instrument_or_provider_borrowing": "PROHIBITED",
    }
    result: dict[str, object] = {
        "schema_version": "g3a-02-final-evidence/1.0.0",
        "work_order": "G3A-02",
        "environment": "DEMO",
        "native_1m_recovery": {
            "target_returned": target_native is not None,
            "returned_timestamps_utc": [utc_text(item.timestamp_utc) for item in native],
            "source_sha256": sha256_bytes(native_path.read_bytes()),
        },
        "second_resolution": {
            "availability": "AVAILABLE",
            "returned_candle_count": len(seconds),
            "returned_by_minute": {
                utc_text(minute) or "": second_counts[minute]
                for minute in (
                    WINDOW_START,
                    TARGET_MINUTE,
                    TARGET_MINUTE.replace(minute=4),
                )
            },
            "source_sha256": source_hash,
            "aggregation_algorithm_version": SECOND_AGGREGATION_VERSION,
            "absent_seconds_filled": 0,
        },
        "adjacent_minute_reconstruction": {
            "broker_decimal_places": decimal_places,
            "comparisons": adjacent_comparison,
            "validation_succeeded": adjacent_valid,
        },
        "target_19_03": {
            "native_present": target_native is not None,
            "returned_second_count": target_seconds,
            "reconstructed": target_reconstructed is not None,
            "handling": "AUTHORITATIVE_GAP_RETAINED",
            "prohibited_fill_count": 0,
        },
        "dataset_qualification": original.get("series"),
        "dataset_fingerprint": original.get("dataset_fingerprint"),
        "gap_aware_replay_policy": policy,
        "exact_scalper_replay_ready": False,
        "classification": "PARTIAL_DATA_WITH_VALID_GAP_POLICY",
        "safety": recovery.get("safety"),
        "remaining_historical_allowance": _get(
            recovery.get("second_resolution"), "allowance_remaining"
        ),
        "source_evidence": {
            "g3a01_evidence_sha256": sha256_bytes(g3a01_evidence.read_bytes()),
            "g3a02_recovery_evidence_sha256": sha256_bytes(recovery_evidence_path.read_bytes()),
        },
    }
    result["evidence_fingerprint"] = fingerprint(result)
    return result


def _normalize_raw(path: Path, resolution: str) -> list[CanonicalCandle]:
    payload = safe_raw_payload(path.read_bytes())
    prices = payload.get("prices")
    if not isinstance(prices, list):
        raise ValueError("raw prices are invalid")
    result = []
    for index, raw in enumerate(prices):
        candle, findings = normalize_candle(
            raw,
            epic=TARGET_EPIC,
            resolution=resolution,
            source_page=1,
            source_index=index,
            source_raw_file=path.name,
            requested_start_utc=WINDOW_START,
            requested_end_utc=WINDOW_END,
        )
        if findings != EMPTY_FINDINGS and findings.rejected_candle_count:
            raise ValueError("recovery raw candle failed normalization")
        if candle is not None:
            result.append(candle)
    return result


def _decimal_places(payload: Mapping[str, object]) -> int:
    snapshot = payload.get("snapshot")
    value = snapshot.get("decimalPlacesFactor") if isinstance(snapshot, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 12:
        raise ValueError("broker decimal precision is missing")
    return value


def _json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path.name}")
    return value


def _get(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, Mapping) else None


def render_markdown(evidence: Mapping[str, object]) -> str:
    lines = [
        "# G3A-02 Gap Recovery Summary",
        "",
        f"Final classification: **{evidence.get('classification')}**",
        "",
        f"- Native 19:03 returned: `{_get(evidence.get('native_1m_recovery'), 'target_returned')}`",
        f"- SECOND availability: `{_get(evidence.get('second_resolution'), 'availability')}`",
        f"- Final 19:03 handling: `{_get(evidence.get('target_19_03'), 'handling')}`",
        f"- Exact Scalper replay ready: `{evidence.get('exact_scalper_replay_ready')}`",
        "",
        "## Adjacent-minute validation",
        "",
    ]
    comparisons = _get(evidence.get("adjacent_minute_reconstruction"), "comparisons")
    if isinstance(comparisons, Mapping):
        for minute, comparison in comparisons.items():
            lines.extend(
                [
                    f"### {minute}",
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
    lines.extend(
        [
            "## GAP_AWARE_REPLAY_V1",
            "",
            "The affected signal is NO_TRADE. Indicator state crossing the gap is invalid. "
            "Warm-up must be rebuilt solely from subsequent authoritative candles, and "
            "trading cannot resume until every required timeframe and indicator is valid. "
            "The gap event must be recorded in replay evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    evidence = analyze(
        g3a01_evidence=args.g3a01_evidence,
        recovery_root=args.recovery_root,
    )
    write_json_create_only(args.output_json, evidence)
    write_create_or_verify(
        args.output_json.with_suffix(".md"),
        render_markdown(evidence).encode("utf-8"),
    )
    print("G3A_02_GAP_ANALYSIS_COMPLETE")
    print(f"classification={evidence['classification']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
