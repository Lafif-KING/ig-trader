"""Mocked tests for the G3A authoritative market-data pipeline."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from src.ig_trader.g3a_data import (
    EMPTY_FINDINGS,
    FROZEN_INSTRUMENTS,
    RESOLUTION_MINUTES,
    GapClassification,
    QualityStatus,
    RawFileEvidence,
    SeriesDataset,
    build_series_manifest,
    canonical_candle_document,
    canonical_json_bytes,
    normalize_candle,
    qualify_series,
    sha256_bytes,
    verify_epic,
)
from tools.g3a_market_data import (
    DEMO_REST_BASE_URL,
    G3AConfig,
    G3AError,
    G3APipeline,
    RawStore,
    SafeG3ARestClient,
    SessionTokens,
    endpoint_is_allowed,
)

START = datetime(2026, 8, 14, 12, tzinfo=UTC)
END = START + timedelta(hours=2)
ACCOUNT = "ACCOUNT-SECRET"


def _price_block(
    bid: float = 1.1,
    ask: float = 1.1002,
    last_traded: float | None = 1.1001,
) -> dict[str, float]:
    result = {"bid": bid, "ask": ask}
    if last_traded is not None:
        result["lastTraded"] = last_traded
    return result


def _raw_candle(timestamp: datetime = START, *, seed: int = 0) -> dict[str, object]:
    shift = seed / 1_000_000
    return {
        "snapshotTime": timestamp.strftime("%Y/%m/%d %H:%M:%S"),
        "snapshotTimeUTC": timestamp.strftime("%Y-%m-%dT%H:%M:%S"),
        "openPrice": _price_block(1.1 + shift, 1.1002 + shift, 1.1001 + shift),
        "highPrice": _price_block(1.101 + shift, 1.1012 + shift, 1.1011 + shift),
        "lowPrice": _price_block(1.099 + shift, 1.0992 + shift, 1.0991 + shift),
        "closePrice": _price_block(1.1005 + shift, 1.1007 + shift, 1.1006 + shift),
        "lastTradedVolume": 10 + seed,
    }


def _canonical(timestamp: datetime = START, *, seed: int = 0):
    candle, findings = normalize_candle(
        _raw_candle(timestamp, seed=seed),
        epic=FROZEN_INSTRUMENTS[0].epic,
        resolution="MINUTE",
        source_page=1,
        source_index=seed,
        source_raw_file="raw/test.json",
        requested_start_utc=START - timedelta(days=3),
        requested_end_utc=END + timedelta(days=3),
    )
    assert candle is not None
    assert findings.rejected_candle_count == 0
    return candle


def _dataset(
    candles,
    *,
    symbol: str = "EURGBP",
    resolution: str = "MINUTE",
    findings=EMPTY_FINDINGS,
    requested_start: datetime | None = None,
    requested_end: datetime | None = None,
) -> SeriesDataset:
    instrument = next(item for item in FROZEN_INSTRUMENTS if item.symbol == symbol)
    normalized = tuple(
        replace(candle, epic=instrument.epic, resolution=resolution) for candle in candles
    )
    interval = timedelta(minutes=RESOLUTION_MINUTES[resolution])
    start = requested_start or min(item.timestamp_utc for item in normalized)
    end = requested_end or max(item.timestamp_utc for item in normalized) + interval
    return SeriesDataset(
        symbol,
        instrument.instrument_name,
        instrument.epic,
        resolution,
        start,
        end,
        normalized,
        findings,
        (RawFileEvidence("raw/source.json", "a" * 64, 100),),
        ({"page_number": 1},),
        9000,
    )


def _config(tmp_path: Path, **changes: Any) -> G3AConfig:
    values = {
        "environment": "demo",
        "base_url": DEMO_REST_BASE_URL,
        "paper_trading": True,
        "data_root": tmp_path / "data",
        "evidence_json": tmp_path / "evidence" / "g3a-data-quality.json",
        "run_id": "test-run",
        "requested_end_utc": datetime(2026, 8, 14, 22, tzinfo=UTC),
        "intervals_per_series": 60,
        "api_key": "API-SECRET",
        "identifier": "IDENTIFIER-SECRET",
        "password": "PASSWORD-SECRET",
        "account_id": ACCOUNT,
        "minimum_request_interval_seconds": 0,
    }
    values.update(changes)
    return G3AConfig(**values)


def _response(
    status: int,
    payload: object | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    request = httpx.Request("GET", DEMO_REST_BASE_URL)
    if payload is None:
        return httpx.Response(status, headers=headers, request=request)
    return httpx.Response(status, json=payload, headers=headers, request=request)


class SequenceClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected network request")
        return self.responses.pop(0)

    def close(self) -> None:
        return None


def test_epic_validation_requires_exact_live_search_and_detail_identity() -> None:
    instrument = FROZEN_INSTRUMENTS[1]
    search = {"markets": [{"epic": instrument.epic, "instrumentName": instrument.instrument_name}]}
    detail = {"instrument": {"epic": instrument.epic, "name": instrument.instrument_name}}

    valid = verify_epic(instrument, search, detail)
    mismatch = verify_epic(
        instrument,
        search,
        {"instrument": {"epic": "WRONG.EPIC", "name": instrument.instrument_name}},
    )

    assert valid.verified is True
    assert valid.reason == "VERIFIED"
    assert mismatch.verified is False
    assert mismatch.reason == "DETAIL_EPIC_MISMATCH"


def test_utc_conversion_ignores_ambiguous_local_time_across_dst_boundary() -> None:
    first_raw = _raw_candle(datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
    second_raw = _raw_candle(datetime(2026, 10, 25, 1, 30, tzinfo=UTC))
    first_raw["snapshotTime"] = "2026/10/25 01:30:00"
    second_raw["snapshotTime"] = "2026/10/25 01:30:00"
    requested_start = datetime(2026, 10, 25, 0, tzinfo=UTC)
    requested_end = datetime(2026, 10, 25, 2, tzinfo=UTC)

    first, first_findings = normalize_candle(
        first_raw,
        epic=FROZEN_INSTRUMENTS[0].epic,
        resolution="MINUTE",
        source_page=1,
        source_index=0,
        source_raw_file="raw/one.json",
        requested_start_utc=requested_start,
        requested_end_utc=requested_end,
    )
    second, second_findings = normalize_candle(
        second_raw,
        epic=FROZEN_INSTRUMENTS[0].epic,
        resolution="MINUTE",
        source_page=1,
        source_index=1,
        source_raw_file="raw/two.json",
        requested_start_utc=requested_start,
        requested_end_utc=requested_end,
    )

    assert first is not None and second is not None
    assert first.timestamp_utc == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert second.timestamp_utc == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert first_findings.timezone_ambiguity_count == 0
    assert second_findings.timezone_ambiguity_count == 0


def test_bid_offer_last_traded_volume_and_source_fields_are_preserved() -> None:
    candle = _canonical()
    document = canonical_candle_document(candle)

    assert document["epic"] == FROZEN_INSTRUMENTS[0].epic
    assert document["resolution"] == "MINUTE"
    assert document["bid_open"] == 1.1
    assert document["offer_close"] == 1.1007
    assert document["last_traded_high"] == 1.1011
    assert document["volume"] == 10.0
    assert document["source_timestamp"] == "2026/08/14 12:00:00"
    assert document["timestamp_utc"] == "2026-08-14T12:00:00+00:00"


def test_invalid_ohlc_and_crossed_market_are_rejected() -> None:
    bad_geometry = _raw_candle()
    bad_geometry["highPrice"] = _price_block(1.0, 1.0002, 1.0001)
    crossed = _raw_candle()
    crossed["openPrice"] = _price_block(1.2, 1.1, 1.15)

    invalid, invalid_findings = normalize_candle(
        bad_geometry,
        epic=FROZEN_INSTRUMENTS[0].epic,
        resolution="MINUTE",
        source_page=1,
        source_index=0,
        source_raw_file="raw/bad.json",
        requested_start_utc=START,
        requested_end_utc=END,
    )
    rejected, crossed_findings = normalize_candle(
        crossed,
        epic=FROZEN_INSTRUMENTS[0].epic,
        resolution="MINUTE",
        source_page=1,
        source_index=0,
        source_raw_file="raw/crossed.json",
        requested_start_utc=START,
        requested_end_utc=END,
    )

    assert invalid is None
    assert invalid_findings.invalid_ohlc_count == 1
    assert rejected is None
    assert crossed_findings.crossed_bid_offer_count == 1
    assert crossed_findings.zero_or_negative_spread_count == 1


def test_duplicates_non_monotonic_and_missing_intervals_are_reported() -> None:
    first = _canonical(START, seed=1)
    later = _canonical(START + timedelta(minutes=2), seed=2)
    duplicate = replace(later, source_index=3)
    out_of_order = _canonical(START + timedelta(minutes=1), seed=4)
    quality = qualify_series((_dataset((first, later, duplicate, out_of_order)),))[
        ("EURGBP", "MINUTE")
    ]

    assert quality.duplicate_timestamps == 1
    assert quality.non_monotonic_timestamps == 1
    assert quality.missing_intervals == 0
    assert quality.status is QualityStatus.DATA_QUALITY_FAILURE


def test_actual_missing_and_expected_weekend_gap_are_distinguished() -> None:
    actual = _dataset(
        (
            _canonical(START, seed=1),
            _canonical(START + timedelta(minutes=2), seed=2),
        )
    )
    friday = datetime(2026, 8, 14, 21, tzinfo=UTC)
    sunday = datetime(2026, 8, 16, 21, tzinfo=UTC)
    weekend = _dataset(
        (
            replace(_canonical(), timestamp_utc=friday),
            replace(_canonical(seed=1), timestamp_utc=sunday),
        ),
        resolution="HOUR",
        requested_start=friday,
        requested_end=sunday + timedelta(hours=1),
    )

    actual_quality = qualify_series((actual,))[("EURGBP", "MINUTE")]
    weekend_quality = qualify_series((weekend,))[("EURGBP", "HOUR")]

    assert actual_quality.detected_gaps[0].classification is (GapClassification.ACTUAL_MISSING_DATA)
    assert actual_quality.status is QualityStatus.PARTIAL_DATA
    assert weekend_quality.detected_gaps[0].classification is (
        GapClassification.EXPECTED_WEEKEND_CLOSURE
    )


def test_mixed_session_and_weekend_gap_is_split_by_interval() -> None:
    friday = datetime(2026, 8, 14, 20, tzinfo=UTC)
    sunday = datetime(2026, 8, 16, 21, tzinfo=UTC)
    datasets = tuple(
        _dataset(
            (
                replace(_canonical(), timestamp_utc=friday),
                replace(_canonical(seed=1), timestamp_utc=sunday),
            ),
            symbol=instrument.symbol,
            resolution="HOUR",
            requested_start=friday,
            requested_end=sunday + timedelta(hours=1),
        )
        for instrument in FROZEN_INSTRUMENTS
    )

    qualities = qualify_series(datasets)
    gaps = qualities[("EURGBP", "HOUR")].detected_gaps

    assert [gap.classification for gap in gaps] == [
        GapClassification.EXPECTED_MARKET_SESSION_GAP,
        GapClassification.EXPECTED_WEEKEND_CLOSURE,
    ]
    assert [gap.missing_intervals for gap in gaps] == [1, 47]


def test_leading_and_trailing_missing_intervals_are_counted() -> None:
    dataset = _dataset(
        (
            _canonical(START + timedelta(minutes=1), seed=1),
            _canonical(START + timedelta(minutes=2), seed=2),
        ),
        requested_start=START,
        requested_end=START + timedelta(minutes=4),
    )

    quality = qualify_series((dataset,))[("EURGBP", "MINUTE")]

    assert quality.missing_intervals == 2
    assert len(quality.detected_gaps) == 2
    assert all(
        gap.classification is GapClassification.ACTUAL_MISSING_DATA for gap in quality.detected_gaps
    )


def test_manifest_and_normalized_fingerprints_are_reproducible() -> None:
    candles = tuple(_canonical(START + timedelta(minutes=index), seed=index) for index in range(60))
    dataset = _dataset(candles)
    quality = qualify_series((dataset,))[("EURGBP", "MINUTE")]
    body_one = b"".join(
        canonical_json_bytes(canonical_candle_document(candle)) + b"\n" for candle in candles
    )
    body_two = b"".join(
        canonical_json_bytes(canonical_candle_document(candle)) + b"\n" for candle in candles
    )
    fixed_time = datetime(2026, 8, 15, 10, tzinfo=UTC)

    first = build_series_manifest(
        dataset,
        quality,
        retrieval_started_at=fixed_time,
        retrieval_completed_at=fixed_time + timedelta(minutes=1),
        normalized_relative_path="normalized/test.jsonl",
        normalized_sha256=sha256_bytes(body_one),
    )
    second = build_series_manifest(
        dataset,
        quality,
        retrieval_started_at=fixed_time,
        retrieval_completed_at=fixed_time + timedelta(minutes=1),
        normalized_relative_path="normalized/test.jsonl",
        normalized_sha256=sha256_bytes(body_two),
    )

    assert body_one == body_two
    assert sha256_bytes(body_one) == sha256_bytes(body_two)
    assert first == second
    assert first["manifest_fingerprint"] == second["manifest_fingerprint"]


def test_pagination_is_bounded_and_raw_cache_resumes_without_network(tmp_path: Path) -> None:
    page_one = {
        "prices": [_raw_candle(START)],
        "metadata": {
            "pageData": {"pageNumber": 1, "totalPages": 2},
            "allowance": {"remainingAllowance": 9999},
        },
    }
    page_two = {
        "prices": [_raw_candle(START + timedelta(minutes=1))],
        "metadata": {
            "pageData": {"pageNumber": 2, "totalPages": 2},
            "allowance": {"remainingAllowance": 9998},
        },
    }
    client = SequenceClient([_response(200, page_one), _response(200, page_two)])
    config = _config(
        tmp_path,
        requested_end_utc=START + timedelta(minutes=60),
    )
    rest = SafeG3ARestClient(config, client=client)
    pipeline = G3APipeline(config, rest=rest)
    pipeline.tokens = SessionTokens("CST-SECRET", "XST-SECRET", ACCOUNT)

    result = pipeline._acquire_series(FROZEN_INSTRUMENTS[0], "MINUTE", timedelta(0))

    assert len(result.candles) == 2
    assert len(result.source_files) == 2
    assert result.allowance_remaining == 9998
    assert len(client.calls) == 2

    cached_client = SequenceClient([])
    cached_rest = SafeG3ARestClient(config, client=cached_client)
    cached_pipeline = G3APipeline(config, rest=cached_rest)
    cached_pipeline.tokens = SessionTokens("CST-SECRET", "XST-SECRET", ACCOUNT)
    cached = cached_pipeline._acquire_series(FROZEN_INSTRUMENTS[0], "MINUTE", timedelta(0))

    assert len(cached.candles) == 2
    assert cached_client.calls == []


def test_rate_limit_is_retried_with_bound_and_retry_after(tmp_path: Path) -> None:
    success = {
        "markets": [
            {
                "epic": FROZEN_INSTRUMENTS[0].epic,
                "instrumentName": FROZEN_INSTRUMENTS[0].instrument_name,
            }
        ]
    }
    client = SequenceClient(
        [
            _response(429, {"errorCode": "rate.limit"}, headers={"Retry-After": "3"}),
            _response(200, success),
        ]
    )
    sleeps: list[float] = []
    config = _config(tmp_path)
    rest = SafeG3ARestClient(config, client=client, sleeper=sleeps.append)
    pipeline = G3APipeline(config, rest=rest)
    pipeline.tokens = SessionTokens("CST-SECRET", "XST-SECRET", ACCOUNT)

    payload, _raw = pipeline._request_market_json(
        "discovery-EURGBP.json",
        "/markets",
        version="1",
        params={"searchTerm": "EURGBP"},
    )

    assert payload == success
    assert sleeps == [3.0]
    assert len(client.calls) == 2


def test_secret_bearing_raw_response_is_rejected_before_write(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "data", "test-run")
    secret_body = json.dumps({"access_token": "MUST-NOT-BE-WRITTEN"}).encode()

    with pytest.raises(G3AError, match="RAW_RESPONSE_CONTAINS_SENSITIVE_FIELD"):
        store.persist("secret.json", secret_body)

    assert not (store.directory / "secret.json").exists()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/positions/otc"),
        ("PUT", "/positions/otc/DEAL-ID"),
        ("DELETE", "/positions/otc/DEAL-ID"),
        ("POST", "/workingorders/otc"),
        ("PUT", "/working-orders/otc/DEAL-ID"),
        ("DELETE", "/workingorders/otc/DEAL-ID"),
    ],
)
def test_every_order_endpoint_is_prohibited_before_network(
    tmp_path: Path, method: str, path: str
) -> None:
    client = SequenceClient([])
    rest = SafeG3ARestClient(_config(tmp_path), client=client)

    with pytest.raises(G3AError, match="ENDPOINT_BLOCKED"):
        rest.request(method, path, version="2")

    assert client.calls == []
    assert rest.order_endpoint_call_count == 0
    assert rest.blocked_endpoint_attempt_count == 1


def test_endpoint_allowlist_has_only_authentication_and_market_data() -> None:
    assert endpoint_is_allowed("POST", "/session", version="2")
    assert endpoint_is_allowed("GET", "/session", version="1")
    assert endpoint_is_allowed(
        "GET",
        "/markets",
        version="1",
        params={"searchTerm": "EURGBP"},
    )
    assert endpoint_is_allowed(
        "GET",
        f"/prices/{FROZEN_INSTRUMENTS[0].epic}",
        version="3",
        params={
            "resolution": "MINUTE",
            "from": "2026-08-14T00:00:00",
            "to": "2026-08-14T01:00:00",
            "pageSize": 500,
            "pageNumber": 1,
        },
    )
    assert not endpoint_is_allowed("GET", "/accounts", version="1")
    assert not endpoint_is_allowed("GET", "/positions", version="2")


def test_g3a_import_does_not_load_execution_or_main() -> None:
    script = (
        "import importlib,sys;"
        "importlib.import_module('tools.g3a_market_data');"
        "assert 'src.ig_trader.execution' not in sys.modules;"
        "assert 'src.ig_trader.main' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


class DynamicSuccessClient:
    """One-page full-universe IG response emulator for the pipeline contract."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, url, kwargs))
        path = urlparse(url).path.removeprefix("/gateway/deal")
        params = kwargs.get("params") or {}
        if method == "POST" and path == "/session":
            return _response(
                200,
                {"currentAccountId": ACCOUNT, "reroutingEnvironment": "DEMO"},
                headers={"CST": "CST-SECRET", "X-SECURITY-TOKEN": "XST-SECRET"},
            )
        if method == "DELETE" and path == "/session":
            return _response(204)
        if path == "/session":
            return _response(200, {"timezoneOffset": 0})
        if path == "/markets":
            symbol = str(params["searchTerm"])
            instrument = next(item for item in FROZEN_INSTRUMENTS if item.symbol == symbol)
            return _response(
                200,
                {
                    "markets": [
                        {
                            "epic": instrument.epic,
                            "instrumentName": instrument.instrument_name,
                        }
                    ]
                },
            )
        if path.startswith("/markets/"):
            epic = path.removeprefix("/markets/")
            instrument = next(item for item in FROZEN_INSTRUMENTS if item.epic == epic)
            return _response(
                200,
                {"instrument": {"epic": epic, "name": instrument.instrument_name}},
            )
        if path.startswith("/prices/"):
            resolution = str(params["resolution"])
            interval = timedelta(minutes=RESOLUTION_MINUTES[resolution])
            start = datetime.fromisoformat(str(params["from"])).replace(tzinfo=UTC)
            candles = [_raw_candle(start + interval * index, seed=index) for index in range(60)]
            return _response(
                200,
                {
                    "prices": candles,
                    "metadata": {
                        "pageData": {"pageNumber": 1, "totalPages": 1},
                        "allowance": {"remainingAllowance": 9280},
                    },
                },
            )
        raise AssertionError(f"unexpected path: {path}")

    def close(self) -> None:
        return None


def test_complete_mock_pipeline_writes_required_evidence_and_zero_order_calls(
    tmp_path: Path,
) -> None:
    client = DynamicSuccessClient()
    config = _config(tmp_path)
    rest = SafeG3ARestClient(config, client=client)
    pipeline = G3APipeline(
        config,
        rest=rest,
        retrieval_started_at=datetime(2026, 8, 15, 10, tzinfo=UTC),
    )

    result = pipeline.run()

    assert result.classification.value == "PASS"
    assert len(result.normalized_paths) == 12
    manifests = [path for path in result.manifest_paths if path.name.endswith("manifest.json")]
    assert len(manifests) == 13
    document = json.loads(config.evidence_json.read_text(encoding="utf-8"))
    assert document["classification"] == "PASS"
    assert document["exact_scalper_replay"]["ready"] is True
    assert document["safety"]["order_endpoint_call_count"] == 0
    assert document["safety"]["execution_adapter_initialized"] is False
    assert document["secret_scan"]["status"] == "PASS"
    assert config.evidence_markdown.is_file()
    combined = config.evidence_json.read_text() + config.evidence_markdown.read_text()
    for secret in config.secret_values():
        assert secret not in combined


def test_offline_requalification_uses_only_immutable_raw_cache(tmp_path: Path) -> None:
    source_client = DynamicSuccessClient()
    source_config = _config(tmp_path)
    source_result = G3APipeline(
        source_config,
        rest=SafeG3ARestClient(source_config, client=source_client),
        retrieval_started_at=datetime(2026, 8, 15, 10, tzinfo=UTC),
    ).run()
    assert source_result.classification.value == "PASS"

    offline_config = replace(
        source_config,
        run_id="test-run-offline",
        raw_cache_run_id=source_config.run_id,
        offline_cache_only=True,
        source_acquisition_evidence=source_config.evidence_json,
        evidence_json=tmp_path / "evidence" / "offline-data-quality.json",
    )
    no_network_client = SequenceClient([])
    result = G3APipeline(
        offline_config,
        rest=SafeG3ARestClient(offline_config, client=no_network_client),
    ).run()

    assert result.classification.value == "PASS"
    assert no_network_client.calls == []
    document = json.loads(offline_config.evidence_json.read_text(encoding="utf-8"))
    assert document["source_acquisition"]["mode"] == ("OFFLINE_IMMUTABLE_RAW_CACHE_REQUALIFICATION")
    assert document["source_acquisition"]["source_market_data_get_call_count"] == 18
    assert document["safety"]["market_data_get_call_count"] == 0
    assert document["safety"]["order_endpoint_call_count"] == 0
