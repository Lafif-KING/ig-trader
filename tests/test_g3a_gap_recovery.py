"""Focused deterministic tests for G3A-02 gap recovery."""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.ig_trader.g3a_data import (
    DERIVED_SECOND_PROVENANCE,
    SECOND_AGGREGATION_VERSION,
    CanonicalCandle,
    aggregate_second_candles,
    canonical_candle_document,
    canonical_candle_from_document,
    compare_bid_offer_fields,
    fingerprint,
)
from tools.g3a_artifact_package import create_package, verify_package
from tools.g3a_gap_recovery import TARGET_EPIC, TARGET_MINUTE
from tools.g3a_market_data import endpoint_is_allowed


def _second(index: int) -> CanonicalCandle:
    timestamp = TARGET_MINUTE + timedelta(seconds=index)
    bid = 0.86000 + index / 100_000
    offer = bid + 0.00020
    return CanonicalCandle(
        epic=TARGET_EPIC,
        resolution="SECOND",
        timestamp_utc=timestamp,
        bid_open=bid,
        bid_high=bid + 0.00002,
        bid_low=bid - 0.00002,
        bid_close=bid + 0.00001,
        offer_open=offer,
        offer_high=offer + 0.00002,
        offer_low=offer - 0.00002,
        offer_close=offer + 0.00001,
        last_traded_open=None,
        last_traded_high=None,
        last_traded_low=None,
        last_traded_close=None,
        volume=None,
        source_timestamp=timestamp.strftime("%Y/%m/%d %H:%M:%S"),
        source_timestamp_utc=timestamp.strftime("%Y-%m-%dT%H:%M:%S"),
        source_page=1,
        source_index=index,
        source_raw_file="raw/seconds.json",
    )


def test_second_resolution_is_allowed_only_on_historical_get() -> None:
    params = {
        "resolution": "SECOND",
        "from": "2026-08-14T19:02:00",
        "to": "2026-08-14T19:04:59",
        "pageSize": 180,
        "pageNumber": 1,
    }

    assert endpoint_is_allowed("GET", f"/prices/{TARGET_EPIC}", version="3", params=params)
    assert not endpoint_is_allowed("POST", f"/prices/{TARGET_EPIC}", version="3", params=params)


def test_complete_second_set_aggregates_deterministically() -> None:
    seconds = tuple(_second(index) for index in range(60))

    first = aggregate_second_candles(
        seconds,
        TARGET_MINUTE,
        epic=TARGET_EPIC,
        source_raw_file="raw/seconds.json",
    )
    second = aggregate_second_candles(
        tuple(reversed(seconds)),
        TARGET_MINUTE,
        epic=TARGET_EPIC,
        source_raw_file="raw/seconds.json",
    )

    assert first == second
    assert first is not None
    assert first.bid_open == seconds[0].bid_open
    assert first.bid_high == max(item.bid_high for item in seconds)
    assert first.bid_low == min(item.bid_low for item in seconds)
    assert first.bid_close == seconds[-1].bid_close
    assert first.offer_open == seconds[0].offer_open
    assert first.offer_high == max(item.offer_high for item in seconds)
    assert first.offer_low == min(item.offer_low for item in seconds)
    assert first.offer_close == seconds[-1].offer_close
    assert first.provenance == DERIVED_SECOND_PROVENANCE
    assert first.aggregation_version == SECOND_AGGREGATION_VERSION
    assert first.source_component_count == 60


def test_incomplete_or_duplicate_second_set_is_never_filled() -> None:
    seconds = tuple(_second(index) for index in range(60))

    missing = aggregate_second_candles(
        seconds[:10] + seconds[11:],
        TARGET_MINUTE,
        epic=TARGET_EPIC,
        source_raw_file="raw/seconds.json",
    )
    duplicate = aggregate_second_candles(
        seconds[:-1] + (seconds[-2],),
        TARGET_MINUTE,
        epic=TARGET_EPIC,
        source_raw_file="raw/seconds.json",
    )

    assert missing is None
    assert duplicate is None


def test_sparse_returned_second_bars_can_be_compared_without_filling() -> None:
    returned = tuple(_second(index) for index in (6, 9, 12, 15))

    reconstructed = aggregate_second_candles(
        returned,
        TARGET_MINUTE,
        epic=TARGET_EPIC,
        source_raw_file="raw/seconds.json",
        require_every_second=False,
    )

    assert reconstructed is not None
    assert reconstructed.bid_open == returned[0].bid_open
    assert reconstructed.bid_close == returned[-1].bid_close
    assert reconstructed.source_component_count == 4


def test_adjacent_validation_reports_all_eight_fields_at_broker_precision() -> None:
    reconstructed = aggregate_second_candles(
        tuple(_second(index) for index in range(60)),
        TARGET_MINUTE,
        epic=TARGET_EPIC,
        source_raw_file="raw/seconds.json",
    )
    assert reconstructed is not None
    native = replace(reconstructed, offer_close=reconstructed.offer_close + 0.00001)

    rows = compare_bid_offer_fields(reconstructed, native, decimal_places=5)

    assert len(rows) == 8
    assert {str(item["field"]) for item in rows} == {
        "bid_open",
        "bid_high",
        "bid_low",
        "bid_close",
        "offer_open",
        "offer_high",
        "offer_low",
        "offer_close",
    }
    assert [item["match"] for item in rows].count(False) == 1


def test_legacy_canonical_row_loads_with_explicit_native_provenance() -> None:
    original = replace(_second(0), resolution="MINUTE", timestamp_utc=TARGET_MINUTE)
    document = canonical_candle_document(original)
    document.pop("provenance")
    document.pop("aggregation_version")
    document.pop("source_component_count")
    document["schema_version"] = "g3a-canonical-candle/1.0.0"

    loaded = canonical_candle_from_document(document)

    assert loaded.provenance == "IG_NATIVE_HISTORICAL"
    assert loaded.aggregation_version is None
    assert loaded.source_component_count is None


def test_target_minute_is_explicit_utc() -> None:
    assert datetime(2026, 8, 14, 19, 3, tzinfo=UTC) == TARGET_MINUTE


def test_external_artifact_package_and_fingerprint_are_reproducible(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "raw.json").write_bytes(b'{"immutable":true}\n')

    first = create_package(
        tmp_path / "package-one",
        artifact_id="g3a-test",
        sources={"source": source},
    )
    second = create_package(
        tmp_path / "package-two",
        artifact_id="g3a-test",
        sources={"source": source},
    )

    assert first["package_fingerprint"] == second["package_fingerprint"]
    assert verify_package(tmp_path / "package-one")["status"] == "PASS"
    assert verify_package(tmp_path / "package-two")["status"] == "PASS"


def test_external_artifact_package_detects_mutation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "raw.json").write_bytes(b'{"immutable":true}\n')
    package = tmp_path / "package"
    create_package(package, artifact_id="g3a-test", sources={"source": source})
    packaged_file = package / "payload" / "source" / "raw.json"
    packaged_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    packaged_file.write_bytes(b'{"immutable":false}\n')

    result = verify_package(package)

    assert result["status"] == "FAIL"
    assert result["reason"] == "PAYLOAD_HASH_OR_FILE_SET_MISMATCH"


def test_final_gap_evidence_and_policy_are_reproducible() -> None:
    path = Path("artifacts/g3a/evidence/g3a-02-summary.json")
    evidence = json.loads(path.read_text(encoding="utf-8"))
    expected_fingerprint = evidence.pop("evidence_fingerprint")
    policy = evidence["gap_aware_replay_policy"]

    assert fingerprint(evidence) == expected_fingerprint
    assert evidence["classification"] == "PARTIAL_DATA_WITH_VALID_GAP_POLICY"
    assert evidence["native_1m_recovery"]["target_returned"] is False
    assert evidence["second_resolution"]["returned_by_minute"]["2026-08-14T19:03:00+00:00"] == 0
    assert evidence["adjacent_minute_reconstruction"]["validation_succeeded"] is True
    assert len(evidence["dataset_qualification"]) == 12
    assert evidence["safety"]["order_endpoint_call_count"] == 0
    assert policy["affected_signal_evaluation"] == "NO_TRADE"
    assert policy["discontinuous_indicator_state"] == "INVALIDATED"
    assert policy["warmup_rebuild"] == "SUBSEQUENT_AUTHORITATIVE_CANDLES_ONLY"
    assert policy["gap_event_recording"] == "REQUIRED_IN_REPLAY_EVIDENCE"
