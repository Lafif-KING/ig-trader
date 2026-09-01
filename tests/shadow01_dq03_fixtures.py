"""Schema-faithful, sanitized DQ-03 handoff fixtures for Shadow01 tests."""

from __future__ import annotations

import json
from pathlib import Path

from src.ig_trader.dq03.artifacts import ARTIFACT_SCHEMA, RESOLVER_VERSION
from src.ig_trader.shadow01.config import ShadowTournamentConfig
from src.ig_trader.shadow01.models import fingerprint

_OBSERVED_AT = "2026-08-29T17:10:00+00:00"


def verified_dq03_documents(config: ShadowTournamentConfig) -> dict[str, dict[str, object]]:
    """Build the three linked DQ-03 documents emitted after Phase 2.

    These are test-only sanitized values.  The helper deliberately does not
    encode or claim any real IG EPIC.
    """

    context: dict[str, object] = {
        "account_identity_fingerprint": "a" * 64,
        "environment": "DEMO",
        "resolver_version": RESOLVER_VERSION,
        "metadata_freshness_hours": 24,
    }
    instruments: list[dict[str, object]] = []
    samples: list[dict[str, object]] = []
    for item in config.universe:
        symbol = str(item["symbol"])
        epic = f"TEST.{symbol}"
        metadata = _metadata(epic)
        rows = _history_rows()
        history_fingerprint = fingerprint(
            {"symbol": symbol, "epic": epic, "resolution": "MINUTE_5", "rows": rows}
        )
        instruments.append(
            {
                "canonical_symbol": symbol,
                "asset_class": item["asset_class"],
                "classification": "VERIFIED",
                "selected_epic": epic,
                "selected_candidate_epic": epic,
                "selected_candidate_name": f"Sanitized {symbol}",
                "display_name": f"Sanitized {symbol}",
                "selected_search_alias": symbol,
                "candidate_count": 1,
                "selection_score": 100,
                "candidate_score": 100,
                "selection_reasons": ["Sanitized DQ-03 test selection."],
                "rejected_candidates": [],
                "candidates": [
                    {
                        "epic": epic,
                        "display_name": f"Sanitized {symbol}",
                        "instrument_type": "CASH",
                        "expiry": "DFB",
                        "market_status": "TRADEABLE",
                        "aliases": [symbol],
                        "score": 100,
                        "selected": True,
                        "reasons": ["Sanitized DQ-03 test selection."],
                        "metadata": metadata,
                        "missing_fields": [],
                    }
                ],
                "metadata": metadata,
                "missing_fields": [],
                "metadata_fingerprint": fingerprint(metadata),
                "data_status": "BROKER_VALIDATED",
                "cost_model_status": "COST_MODEL_INCOMPLETE",
                "broker_validation_fingerprint": history_fingerprint,
                "observed_at_utc": _OBSERVED_AT,
                "error": None,
                "execution_authority": "OFF",
            }
        )
        samples.append(
            {
                "symbol": symbol,
                "epic": epic,
                "resolution": "MINUTE_5",
                "requested_points": 2,
                "returned_points": 2,
                "timestamp_shape_valid": True,
                "ohlc_shape_valid": True,
                "observed_spread_rows": 2,
                "source_fingerprint": history_fingerprint,
                "status": "BROKER_VALIDATED",
                "reason": None,
                "rows": rows,
                "row_count": 2,
                "first_timestamp_utc": rows[0]["timestamp_utc"],
                "last_timestamp_utc": rows[-1]["timestamp_utc"],
                "duplicate_timestamp_count": 0,
                "invalid_row_count": 0,
                "timestamps_monotonic": True,
                "resolution_ordering_valid": True,
                "timestamp_parser_evidence": {"snapshotTimeUTC_iso8601_z": 2},
                "timestamp_policy": "Sanitized test-only DQ-03 history evidence.",
            }
        )
    registry = {
        "schema_version": ARTIFACT_SCHEMA,
        "execution_authority": "OFF",
        "phase": "PHASE_1",
        "latest_augmentation_phase": "PHASE_2",
        "run_context": context,
        "instruments": instruments,
        "strategy_lab_context": {
            "schema_version": "dq03-strategy-lab-context/1.0",
            "execution_authority": "OFF",
            "instruments": [],
        },
    }
    history = {
        "schema_version": ARTIFACT_SCHEMA,
        "execution_authority": "OFF",
        "phase": "PHASE_2",
        "samples": samples,
    }
    manifest = {
        "schema_version": ARTIFACT_SCHEMA,
        "generated_at_utc": _OBSERVED_AT,
        "phase": "PHASE_2",
        "run_context": context,
        "instrument_count": len(instruments),
        "classification_counts": {"VERIFIED": len(instruments)},
        "request_counts": {"demo_create_calls": 0, "demo_close_calls": 0},
        "history_validation_artifact_present": True,
        "streaming_smoke_test": {"status": "NOT_RUN"},
        "demo_create_calls": 0,
        "demo_close_calls": 0,
        "execution_authority": "OFF",
    }
    return {
        "instrument_registry.json": registry,
        "history_validation.json": history,
        "discovery_manifest.json": manifest,
    }


def write_verified_dq03_documents(
    directory: Path, config: ShadowTournamentConfig
) -> dict[str, dict[str, object]]:
    """Write a linked test-only DQ-03 Phase 2 artifact set."""

    documents = verified_dq03_documents(config)
    for name, document in documents.items():
        (directory / name).write_text(json.dumps(document), encoding="utf-8")
    return documents


def _metadata(epic: str) -> dict[str, object]:
    return {
        "epic": epic,
        "display_name": f"Sanitized {epic}",
        "instrument_type": "CASH",
        "expiry": "DFB",
        "market_status": "TRADEABLE",
        "currency": "USD",
        "minimum_deal_size": "1",
        "minimum_stop_distance": "1",
        "decimal_places": 4,
        "one_pip_means": "0.0001",
        "value_of_one_pip": "1",
        "streaming_prices_available": True,
        "bid": "1.0000",
        "offer": "1.0002",
        "spread": "0.0002",
        "controlled_risk_supported": False,
        "minimum_deal_size_unit": "POINTS",
        "minimum_stop_distance_unit": "POINTS",
        "contract_size": "1",
        "lot_size": "1",
        "scaling_factor": 1,
        "missing_fields": [],
        "observed_at_utc": _OBSERVED_AT,
    }


def _history_rows() -> list[dict[str, object]]:
    return [
        _history_row("2026-08-29T17:00:00+00:00", "1.0000"),
        _history_row("2026-08-29T17:05:00+00:00", "1.0001"),
    ]


def _history_row(timestamp: str, close: str) -> dict[str, object]:
    return {
        "timestamp_utc": timestamp,
        "timestamp_source_field": "snapshotTimeUTC",
        "timestamp_parser": "snapshotTimeUTC_iso8601_z",
        "open_mid": close,
        "high_mid": "1.0003",
        "low_mid": "0.9998",
        "close_mid": close,
        "close_bid": "0.9999",
        "close_ask_or_offer": "1.0003",
        "close_side_field": "offer",
        "close_spread": "0.0004",
        "open_source": "bid_offer",
        "high_source": "bid_offer",
        "low_source": "bid_offer",
        "close_source": "bid_offer",
    }
