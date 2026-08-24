"""Regression coverage for DQ-03 real-history and streaming remediation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from src.ig_trader.demo_stream import DemoPriceStream
from src.ig_trader.dq03.__main__ import _format_utc_timestamp
from src.ig_trader.dq03.acquisition import DQ03HistoryAcquirer
from src.ig_trader.dq03.artifacts import write_dq03_artifacts
from src.ig_trader.dq03.models import (
    CandidateEvidence,
    DataStatus,
    DQ03Resolution,
    DQ03Status,
    MarketMetadata,
    RequestCounters,
)
from src.ig_trader.dq03.phases import load_phase_one_resolutions, phase_context
from src.ig_trader.strategy_lab.models import INITIAL_INSTRUMENTS


class _HistoryTransport:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def get_historical_prices(
        self, _epic: str, _resolution: str, _points: int
    ) -> dict[str, object]:
        return {"prices": self.rows}


def _metadata(epic: str = "CS.D.EURUSD.CEFM.IP") -> MarketMetadata:
    return MarketMetadata(
        epic=epic,
        display_name="EUR/USD Mini",
        instrument_type="CURRENCIES",
        expiry="DFB",
        market_status="TRADEABLE",
        currency="USD",
        minimum_deal_size=Decimal("1"),
        minimum_stop_distance=Decimal("2"),
        decimal_places=4,
        one_pip_means=Decimal("0.0001"),
        value_of_one_pip=Decimal("1"),
        streaming_prices_available=True,
        bid=Decimal("1.1000"),
        offer=Decimal("1.1002"),
        controlled_risk_supported=False,
        observed_at=datetime.now(UTC),
    )


def _resolution(*, candidate: bool = False) -> DQ03Resolution:
    metadata = _metadata()
    candidates = (
        (
            CandidateEvidence(
                epic=metadata.epic,
                display_name=metadata.display_name,
                instrument_type=metadata.instrument_type,
                expiry=metadata.expiry,
                market_status=metadata.market_status,
                aliases=("EURUSD",),
                score=100,
                selected=True,
                reasons=("Identity proven",),
                metadata=metadata,
            ),
        )
        if candidate
        else ()
    )
    return DQ03Resolution(
        symbol="EURUSD",
        asset_class=INITIAL_INSTRUMENTS[0].asset_class,
        classification=DQ03Status.VERIFIED,
        selected_epic=metadata.epic,
        display_name="EUR/USD Mini",
        selected_alias="EURUSD",
        candidate_count=1,
        selection_score=100,
        selection_reasons=("Identity proven",),
        candidates=candidates,
        metadata=metadata,
        data_status=DataStatus.DATA_NOT_AVAILABLE,
        observed_at=datetime.now(UTC),
    )


def _bar(
    timestamp: str,
    *,
    field: str = "snapshotTimeUTC",
    side: str = "ask",
    last_traded: bool = False,
    close_bid: str = "100.5",
) -> dict[str, object]:
    if last_traded:
        return {
            field: timestamp,
            "openPrice": {"lastTraded": "101"},
            "highPrice": {"lastTraded": "102"},
            "lowPrice": {"lastTraded": "100"},
            "closePrice": {"lastTraded": "101.5"},
        }
    return {
        field: timestamp,
        "openPrice": {"bid": "100", side: "102"},
        "highPrice": {"bid": "101", side: "103"},
        "lowPrice": {"bid": "99", side: "101"},
        "closePrice": {"bid": close_bid, side: "102.5"},
    }


def _sample(rows: list[dict[str, object]], *, offset_hours: int | None = None):
    updated, samples = DQ03HistoryAcquirer(
        _HistoryTransport(rows), RequestCounters(), snapshot_time_utc_offset_hours=offset_hours
    ).validate_verified((_resolution(),), points=2)
    return updated[0], samples[0]


@pytest.mark.parametrize(
    ("field", "first", "second", "parser", "offset_hours"),
    [
        (
            "snapshotTimeUTC",
            "2026-08-24T08:00:00Z",
            "2026-08-24T08:05:00Z",
            "snapshotTimeUTC_iso8601_z",
            None,
        ),
        (
            "snapshotTimeUTC",
            "2026-08-24T08:00:00",
            "2026-08-24T08:05:00",
            "snapshotTimeUTC_iso8601_naive_utc",
            None,
        ),
        (
            "snapshotTimeUTC",
            "2026/08/24 08:00:00",
            "2026/08/24 08:05:00",
            "snapshotTimeUTC_slash_naive_utc",
            None,
        ),
        (
            "snapshotTime",
            "2026/08/24 10:00:00",
            "2026/08/24 10:05:00",
            "snapshotTime_slash_session_offset_hours_+2",
            2,
        ),
    ],
)
def test_ig_timestamp_contracts_normalize_to_utc(
    field: str, first: str, second: str, parser: str, offset_hours: int | None
) -> None:
    updated, sample = _sample(
        [_bar(first, field=field), _bar(second, field=field)], offset_hours=offset_hours
    )

    assert updated.data_status is DataStatus.BROKER_VALIDATED
    assert sample.status is DataStatus.BROKER_VALIDATED
    assert sample.timestamp_parser_evidence == ((parser, 2),)
    assert sample.normalized_rows[0].timestamp_utc.tzinfo is UTC


def test_naive_snapshot_time_without_broker_session_offset_fails_closed() -> None:
    _, sample = _sample(
        [
            _bar("2026/08/24 10:00:00", field="snapshotTime"),
            _bar("2026/08/24 10:05:00", field="snapshotTime"),
        ]
    )

    assert sample.status is DataStatus.DATA_QUALITY_FAIL
    assert "unnormalizable timestamp" in (sample.reason or "")


def test_streaming_timestamp_formatter_is_utc_for_an_explicit_local_offset() -> None:
    value = datetime(2026, 8, 24, 11, 30, 0, 123456, tzinfo=timezone(timedelta(hours=2)))

    assert _format_utc_timestamp(value) == "2026-08-24T09:30:00.123456Z"


@pytest.mark.parametrize("side", ["ask", "offer"])
def test_ig_bid_side_ohlc_produces_mid_and_spread(side: str) -> None:
    _, sample = _sample(
        [
            _bar("2026-08-24T08:00:00Z", side=side),
            _bar("2026-08-24T08:05:00Z", side=side),
        ]
    )

    row = sample.normalized_rows[0]
    assert sample.status is DataStatus.BROKER_VALIDATED
    assert row.open_mid == Decimal("101")
    assert row.close_bid == Decimal("100.5")
    assert row.close_ask_or_offer == Decimal("102.5")
    assert row.close_side_field == side
    assert row.close_spread == Decimal("2.0")


def test_last_traded_is_explicit_fallback_when_no_valid_two_sided_price_exists() -> None:
    _, sample = _sample(
        [
            _bar("2026-08-24T08:00:00Z", last_traded=True),
            _bar("2026-08-24T08:05:00Z", last_traded=True),
        ]
    )

    assert sample.status is DataStatus.BROKER_VALIDATED
    assert sample.observed_spread_rows == 0
    assert sample.normalized_rows[0].close_source == "lastTraded_fallback"
    assert sample.normalized_rows[0].close_spread is None


def test_invalid_positive_ohlc_and_duplicate_timestamps_fail_closed() -> None:
    duplicate = _bar("2026-08-24T08:00:00Z")
    invalid = _bar("2026-08-24T08:00:00Z", close_bid="0")
    _, sample = _sample([duplicate, invalid])

    assert sample.status is DataStatus.DATA_QUALITY_FAIL
    assert sample.duplicate_timestamp_count == 0  # Invalid rows do not create trusted timestamps.
    assert sample.invalid_row_count == 1
    assert "timestamps are not strictly increasing" in (sample.reason or "")


def test_duplicate_trusted_timestamps_are_rejected() -> None:
    _, sample = _sample([_bar("2026-08-24T08:00:00Z"), _bar("2026-08-24T08:00:00Z")])

    assert sample.status is DataStatus.DATA_QUALITY_FAIL
    assert sample.duplicate_timestamp_count == 1
    assert "duplicate timestamp" in (sample.reason or "")


def test_normalized_broker_fingerprint_is_stable_and_price_sensitive() -> None:
    _, first = _sample([_bar("2026-08-24T08:00:00Z"), _bar("2026-08-24T08:05:00Z")])
    _, same = _sample([_bar("2026-08-24T08:00:00Z"), _bar("2026-08-24T08:05:00Z")])
    _, changed = _sample(
        [
            _bar("2026-08-24T08:00:00Z"),
            _bar("2026-08-24T08:05:00Z", close_bid="100.6"),
        ]
    )

    assert first.source_fingerprint == same.source_fingerprint
    assert first.source_fingerprint != changed.source_fingerprint


def test_phase_two_and_three_augment_phase_one_evidence(tmp_path: Path) -> None:
    context = phase_context("DEMO-TEST")
    original = _resolution(candidate=True)
    write_dq03_artifacts(tmp_path, (original,), RequestCounters(), run_context=context)
    loaded = load_phase_one_resolutions(tmp_path, context)
    updated, sample = DQ03HistoryAcquirer(
        _HistoryTransport([_bar("2026-08-24T08:00:00Z"), _bar("2026-08-24T08:05:00Z")]),
        RequestCounters(),
    ).validate_verified(loaded, points=2)
    write_dq03_artifacts(
        tmp_path,
        updated,
        RequestCounters(),
        history_samples=sample,
        phase="PHASE_2",
        run_context=context,
    )
    history_before = (tmp_path / "history_validation.json").read_text(encoding="utf-8")
    write_dq03_artifacts(
        tmp_path,
        updated,
        RequestCounters(),
        streaming_result={"status": "PASS", "fresh_quote_count": 1},
        phase="PHASE_3",
        run_context=context,
    )

    registry = json.loads((tmp_path / "instrument_registry.json").read_text(encoding="utf-8"))
    candidates = json.loads((tmp_path / "candidate_evidence.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "discovery_manifest.json").read_text(encoding="utf-8"))
    instrument = registry["instruments"][0]
    assert instrument["selection_reasons"] == ["Identity proven"]
    assert instrument["candidates"][0]["epic"] == "CS.D.EURUSD.CEFM.IP"
    assert instrument["broker_validation"]["source_fingerprint"] == sample[0].source_fingerprint
    assert candidates["instruments"][0]["candidates"][0]["selected"] is True
    assert (tmp_path / "history_validation.json").read_text(encoding="utf-8") == history_before
    assert manifest["phase"] == "PHASE_3"
    assert manifest["streaming_smoke_test"]["status"] == "PASS"


def test_lightstreamer_uses_server_address_and_records_bid_offer_without_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clients: list[Any] = []

    class Connection:
        def __init__(self) -> None:
            self.user: str | None = None
            self.password: str | None = None

        def setUser(self, value: str) -> None:  # noqa: N802
            self.user = value

        def setPassword(self, value: str) -> None:  # noqa: N802
            self.password = value

    class Client:
        def __init__(self, server_address: str, adapter_set: str | None) -> None:
            self.server_address = server_address
            self.adapter_set = adapter_set
            self.connectionDetails = Connection()
            self.subscription: Any = None
            self.listener: Any = None
            clients.append(self)

        def addListener(self, listener: Any) -> None:  # noqa: N802
            self.listener = listener

        def connect(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

        def subscribe(self, subscription: Any) -> None:
            self.subscription = subscription

    class Subscription:
        def __init__(self, _mode: str, items: list[str], fields: list[str]) -> None:
            self.items = items
            self.fields = fields
            self.listener: Any = None

        def addListener(self, listener: Any) -> None:  # noqa: N802
            self.listener = listener

    class Update:
        def getItemName(self) -> str:  # noqa: N802
            return "MARKET:CS.TEST"

        def getValue(self, field: str) -> str:  # noqa: N802
            return {"BID": "1.1000", "OFFER": "1.1002"}[field]

    class Tokens:
        account_id = "DEMO-TEST"
        cst = "cst-value"
        x_security_token = "xst-value"

    stream = DemoPriceStream(
        endpoint="https://demo-stream.example.test",
        session=Tokens(),
        rest_demo_proven=True,
        client_factory=Client,
        subscription_factory=Subscription,
    )
    stream.connect()
    stream.subscribe_prices(("CS.TEST",))
    clients[0].listener.onStatusChange("CONNECTED:HTTP-STREAMING")
    clients[0].subscription.listener.onSubscription()
    clients[0].subscription.listener.onItemUpdate(Update())
    quote = stream.quote("CS.TEST", maximum_age=timedelta(seconds=1))

    assert clients[0].server_address == "https://demo-stream.example.test"
    assert clients[0].adapter_set is None
    assert clients[0].connectionDetails.user == "DEMO-TEST"
    assert clients[0].connectionDetails.password == "CST-cst-value|XST-xst-value"
    assert stream.connection_confirmed is True
    assert stream.subscription_confirmed is True
    assert clients[0].subscription.items == ["MARKET:CS.TEST"]
    assert clients[0].subscription.fields == ["BID", "OFFER"]
    assert quote is not None and quote.bid == Decimal("1.1000") and quote.offer == Decimal("1.1002")
    assert "cst-value" not in caplog.text and "xst-value" not in caplog.text
