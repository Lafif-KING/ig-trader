"""Offline contracts for SL-04 local structured-history research."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.ig_trader.sl02.contracts import (
    BrokerEvidence,
    BrokerValidationPoint,
    CostEvidence,
    DatasetDepthStatus,
)
from src.ig_trader.sl02.costs import friction_model
from src.ig_trader.sl02.evidence import compare_with_broker_sample
from src.ig_trader.sl02.history import ExternalHistoryUnavailable
from src.ig_trader.sl03.history import ProviderProvenance, ResearchDataset, resample_complete
from src.ig_trader.sl03.quality import audit_dataset
from src.ig_trader.sl04.dukascopy import (
    DukascopyApiKeyRequired,
    DukascopyOfficialClient,
    DukascopyStructuredHistorySource,
)
from src.ig_trader.sl04.history import (
    EXPORT_SCHEMA,
    DukascopyHistoricalExportSource,
    SL04SourcePriority,
)
from src.ig_trader.sl04.local_csv import (
    EXPECTED_SCHEMA,
    LOCAL_PROVIDER,
    LocalDukascopyGoCsvSource,
)
from src.ig_trader.sl04.runner import SL04Runner
from src.ig_trader.strategy_lab.data import (
    DataContractError,
    GapClassification,
    LabCandle,
    SourceQuality,
    build_dataset,
)
from src.ig_trader.strategy_lab.models import AssetClass, Timeframe
from src.ig_trader.strategy_lab.segments import GapSafeResearchSegmenter

FIXED_NOW = datetime(2026, 1, 5, 12, tzinfo=UTC)


def _client(
    cache_directory: Path, handler, *, api_key: str | None = None
) -> DukascopyOfficialClient:
    return DukascopyOfficialClient(
        cache_directory=cache_directory,
        api_key=api_key,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        backoff_seconds=0,
        sleep=lambda _: None,
        now=lambda: FIXED_NOW,
    )


def _instrument_response() -> httpx.Response:
    return httpx.Response(200, json=[{"id": 101, "symbol": "EUR/USD", "name": "Euro Dollar"}])


def _side_rows(request: httpx.Request) -> list[dict[str, object]]:
    parameters = dict(request.url.params)
    start = datetime.fromtimestamp(int(parameters["start"]) / 1000, tz=UTC)
    end = datetime.fromtimestamp(int(parameters["end"]) / 1000, tz=UTC)
    limit = int(parameters["count"])
    rows = []
    point = start
    while point < end and len(rows) < limit:
        base = Decimal("1.10000") + Decimal(len(rows)) / Decimal("100000")
        adjustment = Decimal("0.00010") if parameters["offerSide"] == "A" else Decimal("0")
        rows.append(
            {
                "timestamp": int(point.timestamp() * 1000),
                "open": str(base + adjustment),
                "high": str(base + adjustment + Decimal("0.00002")),
                "low": str(base + adjustment - Decimal("0.00002")),
                "close": str(base + adjustment + Decimal("0.00001")),
                "volume": "1",
            }
        )
        point += timedelta(minutes=1)
    return rows


def test_preflight_accepts_anonymous_provider_and_resolves_only_official_ids(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return (
            _instrument_response()
            if request.url.params["path"] == "api/instruments"
            else httpx.Response(200, json=[])
        )

    client = _client(tmp_path, handler)
    preflight = client.preflight()

    assert preflight.auth_mode == "ANONYMOUS_ACCEPTED_BY_PROVIDER"
    assert preflight.instrument_resolution_count == 1
    assert client.resolve("EURUSD").provider_id == 101


def test_preflight_reports_key_required_without_storing_or_printing_a_key(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "auth required"})

    client = _client(tmp_path, handler)
    with pytest.raises(DukascopyApiKeyRequired, match="DUKASCOPY_API_KEY_REQUIRED"):
        client.preflight()
    assert not list(tmp_path.rglob("*"))


def test_cache_redacts_api_key_from_all_artifact_safe_documents(tmp_path: Path) -> None:
    secret = "must-never-reach-a-cache"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("key") == secret
        if request.url.params["path"] == "api/instruments":
            return _instrument_response()
        return httpx.Response(
            200,
            json={
                "diagnostic": secret,
                "candles": [
                    {
                        "timestamp": int(FIXED_NOW.timestamp() * 1000),
                        "open": "1.1",
                        "high": "1.2",
                        "low": "1.0",
                        "close": "1.1",
                    }
                ],
            },
        )

    client = _client(tmp_path, handler, api_key=secret)
    instrument = client.resolve("EURUSD")
    client.fetch_pages(
        instrument=instrument,
        source_timeframe=Timeframe.M1,
        offer_side="B",
        start_utc=FIXED_NOW,
        end_utc=FIXED_NOW + timedelta(minutes=1),
    )

    assert all(secret not in path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
    cached = json.loads((tmp_path / "instrument_resolver.json").read_text(encoding="utf-8"))
    assert cached["endpoint"] == "https://freeserv.dukascopy.com/2.0/?path=api/instruments"


def test_historical_pagination_uses_fixed_half_open_5000_row_windows_and_resumes(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["path"] == "api/instruments":
            return _instrument_response()
        requests.append(dict(request.url.params))
        return httpx.Response(200, json=_side_rows(request))

    client = _client(tmp_path, handler)
    instrument = client.resolve("EURUSD")
    start = datetime(2026, 1, 5, tzinfo=UTC)
    pages = client.fetch_pages(
        instrument=instrument,
        source_timeframe=Timeframe.M1,
        offer_side="B",
        start_utc=start,
        end_utc=start + timedelta(minutes=5001),
    )
    assert len(pages) == 2
    assert [item["count"] for item in requests] == ["5000", "5000"]
    assert requests[0]["end"] == requests[1]["start"]
    before = len(requests)
    cached = client.fetch_pages(
        instrument=instrument,
        source_timeframe=Timeframe.M1,
        offer_side="B",
        start_utc=start,
        end_utc=start + timedelta(minutes=5001),
    )
    assert len(cached) == 2
    assert len(requests) == before
    assert client.accounting.cache_hits >= 2


def test_bid_ask_matching_builds_deterministic_mid_and_rejects_mismatch(tmp_path: Path) -> None:
    def aligned_handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["path"] == "api/instruments":
            return _instrument_response()
        return httpx.Response(200, json=_side_rows(request))

    client = _client(tmp_path, aligned_handler)
    source = DukascopyStructuredHistorySource(cache_directory=tmp_path, client=client)
    acquired = source.acquire(
        symbol="EURUSD",
        asset_class=AssetClass.FX,
        source_timeframe=Timeframe.M1,
        start_utc=datetime(2026, 1, 5, tzinfo=UTC),
        end_utc=datetime(2026, 1, 5, 0, 2, tzinfo=UTC),
    )
    assert acquired.dataset.candles[0].open == Decimal("1.10005")
    assert acquired.dataset.candles[0].spread == Decimal("0.00010")

    def mismatched_handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["path"] == "api/instruments":
            return _instrument_response()
        rows = _side_rows(request)
        if request.url.params["offerSide"] == "A":
            rows[0]["timestamp"] = int(datetime(2026, 1, 5, 0, 3, tzinfo=UTC).timestamp() * 1000)
        return httpx.Response(200, json=rows)

    mismatch_client = _client(tmp_path / "mismatch", mismatched_handler)
    mismatch_source = DukascopyStructuredHistorySource(
        cache_directory=tmp_path / "mismatch", client=mismatch_client
    )
    with pytest.raises(DataContractError, match="DUKASCOPY_BID_ASK_TIMESTAMP_MISMATCH"):
        mismatch_source.acquire(
            symbol="EURUSD",
            asset_class=AssetClass.FX,
            source_timeframe=Timeframe.M1,
            start_utc=datetime(2026, 1, 5, tzinfo=UTC),
            end_utc=datetime(2026, 1, 5, 0, 2, tzinfo=UTC),
        )


def test_resampling_omits_a_bucket_with_a_missing_required_minute() -> None:
    candles = tuple(
        _candle(datetime(2026, 1, 5, tzinfo=UTC) + timedelta(minutes=index))
        for index in (0, 1, 2, 3, 5, 6, 7, 8, 9)
    )
    minute = build_dataset(candles, source_documents=("sl04-missing-minute",))
    resampled = resample_complete(minute, Timeframe.M5)
    assert len(resampled.candles) == 1
    assert resampled.candles[0].timestamp_utc == datetime(2026, 1, 5, 0, 5, tzinfo=UTC)


def test_local_dukascopy_go_csv_is_validated_then_resampled_without_network(tmp_path: Path) -> None:
    _write_local_csv(tmp_path / "m1_90d" / "eurusd.csv", Timeframe.M1, count=6)
    _write_local_csv(tmp_path / "h1_2y" / "eurusd.csv", Timeframe.H1, count=4)
    source = LocalDukascopyGoCsvSource(tmp_path)

    minute = source.load("EURUSD", Timeframe.M5, AssetClass.FX)
    hour = source.load("EURUSD", Timeframe.H4, AssetClass.FX)
    validation = source.import_validation_document()
    resampling = source.resampling_document()

    assert minute.provenance.provider == LOCAL_PROVIDER
    assert minute.provenance.parent_dataset_fingerprint is not None
    assert len(minute.dataset.candles) == 1
    assert len(hour.dataset.candles) == 1
    assert validation["files_discovered"] == 2
    assert validation["files_accepted"] == 2
    assert validation["files_rejected"] == 0
    assert validation["raw_rows_imported"] == 10
    assert any(item["target_timeframe"] == "5M" for item in resampling["records"])
    assert any(item["target_timeframe"] == "4H" for item in resampling["records"])


def test_resampling_records_source_gap_to_derived_omission_lineage(tmp_path: Path) -> None:
    _write_local_csv(tmp_path / "m1_90d" / "eurusd.csv", Timeframe.M1, count=10, missing={4})
    source = LocalDukascopyGoCsvSource(tmp_path)

    source.load("EURUSD", Timeframe.M5, AssetClass.FX)
    record = source.resampling_document()["records"][0]

    assert record["root_source_gap_count"] == 1
    assert record["derived_omitted_bucket_count"] == 1
    assert record["derived_omissions"][0]["classification"] == "DERIVED_BUCKET_OMITTED"
    assert record["derived_omissions"][0]["root_source_gap_ids"] == ["SOURCE_GAP_00001"]


def test_gap_safe_segmenter_splits_hard_gap_without_bridging_state() -> None:
    start = datetime(2026, 1, 5, tzinfo=UTC)
    source = build_dataset(
        tuple(_candle(start + timedelta(minutes=index)) for index in range(701) if index != 350),
        source_documents=("gap-safe-segments",),
    )

    segmented = GapSafeResearchSegmenter().segment(audit_dataset(source, AssetClass.FX).dataset)
    partitions = segmented.partitions()

    assert len(segmented.segments) == 2
    assert len(segmented.eligible_segments) == 2
    assert segmented.hard_boundaries[0].missing_intervals == 1
    assert partitions.ready
    assert all(
        not dataset.has_quality_failure
        for phase in (partitions.development, partitions.validation, partitions.untouched_test)
        for dataset in phase
    )


def test_before_after_uses_the_required_local_pre_segmentation_baseline(tmp_path: Path) -> None:
    runner = SL04Runner(
        artifact_directory=tmp_path,
        dq03_directory=tmp_path,
        yahoo_cache_directory=tmp_path,
    )

    comparison = runner._before_after(SimpleNamespace(dataset_count=80))

    assert comparison["before_source"] == "OPERATOR_REPORTED_PRE_SEGMENTATION_LOCAL_SL04_REPLAY"
    assert comparison["before"]["simulated_combinations"] == 5


def test_local_dukascopy_go_midpoint_mismatch_rejects_the_affected_dataset(tmp_path: Path) -> None:
    path = tmp_path / "m1_90d" / "eurusd.csv"
    _write_local_csv(path, Timeframe.M1, count=5, midpoint_mismatch=True)
    source = LocalDukascopyGoCsvSource(tmp_path)

    source.prepare()

    validation = source.import_validation_document()
    assert validation["files_rejected"] == 1
    assert "MIDPOINT_MISMATCH" in validation["files"][0]["reason"]
    with pytest.raises(ExternalHistoryUnavailable, match="MIDPOINT_MISMATCH"):
        source.load("EURUSD", Timeframe.M5, AssetClass.FX)


def test_documented_manual_export_is_ingested_without_network(tmp_path: Path) -> None:
    export = tmp_path / "eurusd_1m_dukascopy_export.json"
    export.write_text(
        json.dumps(
            {
                "schema_version": EXPORT_SCHEMA,
                "provider_symbol": "EUR/USD",
                "candles": [
                    {
                        "timestamp_utc": "2026-01-05T00:00:00Z",
                        "bid_open": "1.1",
                        "bid_high": "1.2",
                        "bid_low": "1.0",
                        "bid_close": "1.15",
                        "ask_open": "1.101",
                        "ask_high": "1.201",
                        "ask_low": "1.001",
                        "ask_close": "1.151",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    acquired = DukascopyHistoricalExportSource(tmp_path).load("EURUSD", Timeframe.M1, AssetClass.FX)
    assert acquired.provenance.provider == "DUKASCOPY_OFFICIAL_HISTORICAL_EXPORT"
    assert acquired.dataset.candles[0].close == Decimal("1.1505")


def test_source_priority_prefers_local_dukascopy_dataset_before_yahoo(tmp_path: Path) -> None:
    expected = _research_dataset()

    class FakeLocalDukascopy:
        def load(self, *args):
            return expected

    priority = SL04SourcePriority(
        local_csv=FakeLocalDukascopy(),  # type: ignore[arg-type]
        export_directory=tmp_path,
        yahoo_cache_directory=tmp_path,
    )
    assert priority.load("EURUSD", Timeframe.M1, AssetClass.FX) is expected


def test_alignment_divergence_stays_fail_closed_and_native_stop_conversion_is_unchanged() -> None:
    dataset = build_dataset((_candle(datetime(2026, 1, 5, tzinfo=UTC)),), source_documents=("x",))
    evidence = BrokerEvidence(
        symbol="EURUSD",
        epic="CS.D.EURUSD.MINI.IP",
        metadata_fingerprint="a" * 64,
        broker_validation_fingerprint="b" * 64,
        data_status="BROKER_VALIDATED",
        cost_model_status="COMPLETE",
        pip_or_tick_size=Decimal("0.0001"),
        minimum_deal_size=Decimal("1"),
        minimum_stop_distance=Decimal("10"),
        observed_spread=Decimal("0.0001"),
        currency="USD",
        points=(
            BrokerValidationPoint(
                datetime(2026, 1, 5, tzinfo=UTC), Decimal("1.2500"), Decimal("0.0001")
            ),
        ),
        observed_spreads=(Decimal("0.0001"),),
        minimum_stop_distance_value=Decimal("10"),
        minimum_stop_distance_unit="POINTS",
        decimal_places=5,
        scaling_factor=1,
    )
    assert (
        compare_with_broker_sample(dataset, evidence).status.value == "MATERIAL_SOURCE_DIVERGENCE"
    )
    friction = friction_model(
        evidence,
        CostEvidence(
            symbol="EURUSD",
            metadata_fingerprint="a" * 64,
            base_spread=Decimal("0.0001"),
            slippage=Decimal("0"),
            commission_price_equivalent=Decimal("0"),
            allowed_utc_hours=frozenset(range(24)),
            evidence_basis="test",
        ),
        stress_multiplier=Decimal("1"),
    )
    assert friction is not None
    assert friction.minimum_stop_distance == Decimal("0.0010")


def _candle(timestamp: datetime) -> LabCandle:
    return LabCandle(
        instrument="EURUSD",
        timestamp_utc=timestamp,
        timeframe=Timeframe.M1,
        open=Decimal("1.1000"),
        high=Decimal("1.1002"),
        low=Decimal("1.0998"),
        close=Decimal("1.1001"),
        spread=Decimal("0.0001"),
        volume=Decimal("1"),
        source="SYNTHETIC_TEST_ONLY",
        source_quality=SourceQuality.SYNTHETIC_TEST_ONLY,
        gap_classification=GapClassification.NONE,
        synthetic=True,
    )


def _research_dataset() -> ResearchDataset:
    dataset = build_dataset((_candle(datetime(2026, 1, 5, tzinfo=UTC)),), source_documents=("p",))
    return ResearchDataset(
        dataset=dataset,
        provenance=ProviderProvenance(
            provider="TEST",
            provider_symbol="EUR/USD",
            acquisition_timestamp_utc=FIXED_NOW,
            source_url="LOCAL",
            raw_source_fingerprint="a" * 64,
            normalized_fingerprint=dataset.dataset_fingerprint,
            license_source_note="test",
        ),
        depth_status=DatasetDepthStatus.LOW_DATA_DEPTH,
        cached=True,
    )


def _write_local_csv(
    path: Path,
    timeframe: Timeframe,
    *,
    count: int,
    midpoint_mismatch: bool = False,
    missing: set[int] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 1, 5, tzinfo=UTC)
    interval = timedelta(minutes=1) if timeframe is Timeframe.M1 else timedelta(hours=1)
    rows = [",".join(EXPECTED_SCHEMA)]
    for index in range(count):
        if missing is not None and index in missing:
            continue
        bid_open = Decimal("1.10000") + Decimal(index) / Decimal("100000")
        bid_high = bid_open + Decimal("0.00010")
        bid_low = bid_open - Decimal("0.00010")
        bid_close = bid_open + Decimal("0.00005")
        ask_open = bid_open + Decimal("0.00010")
        ask_high = bid_high + Decimal("0.00010")
        ask_low = bid_low + Decimal("0.00010")
        ask_close = bid_close + Decimal("0.00010")
        mid_open = (bid_open + ask_open) / 2
        if midpoint_mismatch and index == 0:
            mid_open += Decimal("0.000001")
        values = (
            (start + interval * index).isoformat().replace("+00:00", "Z"),
            mid_open,
            (bid_high + ask_high) / 2,
            (bid_low + ask_low) / 2,
            (bid_close + ask_close) / 2,
            Decimal("0.00010"),
            Decimal("1"),
            bid_open,
            bid_high,
            bid_low,
            bid_close,
            ask_open,
            ask_high,
            ask_low,
            ask_close,
        )
        rows.append(",".join(str(value) for value in values))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
