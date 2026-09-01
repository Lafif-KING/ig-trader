"""Synthetic-only tests for the canonical Shadow01 IG Price-stream quote."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import src.ig_trader.shadow01.live_readonly_smoke as smoke_module
from src.ig_trader.shadow01.live_quote import (
    build_ig_price_stream_quote,
    stream_timestamp_status,
)

NOW = datetime(2026, 9, 2, 21, 10, tzinfo=UTC)


def _quote(**overrides: object):
    values: dict[str, object] = {
        "epic": "CS.TEST.EPIC",
        "symbol": "EURUSD",
        "bid_value": "1.0000",
        "ask_value": "1.0002",
        "timestamp_milliseconds": int((NOW - timedelta(seconds=30)).timestamp() * 1000),
        "market_state": "DEAL",
        "observed_at": NOW,
        "maximum_age_seconds": 60,
    }
    values.update(overrides)
    return build_ig_price_stream_quote(**values)


def test_stream_tier_one_fields_build_a_fresh_canonical_live_quote() -> None:
    quote = _quote(timestamp_milliseconds=float((NOW - timedelta(seconds=30)).timestamp() * 1000))

    assert quote.source == "IG_PRICE_STREAM"
    assert quote.quality == "VALID_QUOTE"
    assert quote.reason_codes == ()
    assert quote.quote_age_seconds == 30.0
    assert "1.0000" not in repr(quote)


def test_stream_accepts_the_live_proven_ascii_millisecond_string() -> None:
    timestamp = str(int((NOW - timedelta(seconds=30)).timestamp() * 1000))

    quote = _quote(timestamp_milliseconds=timestamp)

    assert quote.quality == "VALID_QUOTE"
    assert quote.reason_codes == ()
    assert quote.quote_age_seconds == 30.0
    assert timestamp not in repr(quote)


@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    (
        ({"bid_value": None}, "SHADOW01_LIVE_QUOTE_BID_UNAVAILABLE"),
        ({"bid_value": "not-a-number"}, "SHADOW01_LIVE_QUOTE_BID_UNAVAILABLE"),
        ({"ask_value": None}, "SHADOW01_LIVE_QUOTE_ASK_UNAVAILABLE"),
        ({"ask_value": "not-a-number"}, "SHADOW01_LIVE_QUOTE_ASK_UNAVAILABLE"),
        ({"bid_value": float("nan")}, "SHADOW01_LIVE_QUOTE_BID_UNAVAILABLE"),
        ({"ask_value": float("inf")}, "SHADOW01_LIVE_QUOTE_ASK_UNAVAILABLE"),
        ({"bid_value": "2.0", "ask_value": "1.0"}, "SHADOW01_LIVE_QUOTE_SPREAD_INVALID"),
    ),
)
def test_stream_quote_rejects_missing_or_invalid_tier_one_prices(
    overrides: dict[str, object],
    reason_code: str,
) -> None:
    quote = _quote(**overrides)

    assert quote.quality == "UNAVAILABLE"
    assert quote.reason_codes == (reason_code,)
    assert quote.bid is None
    assert quote.ask is None
    assert quote.timestamp_utc is None


@pytest.mark.parametrize(
    ("timestamp", "quality", "reason_code"),
    (
        (None, "UNAVAILABLE", "SHADOW01_STREAM_TIMESTAMP_MISSING"),
        ("milliseconds-as-text", "UNAVAILABLE", "SHADOW01_STREAM_TIMESTAMP_SCHEMA_UNSUPPORTED"),
        (float("nan"), "UNAVAILABLE", "SHADOW01_STREAM_TIMESTAMP_INVALID"),
        (float("inf"), "UNAVAILABLE", "SHADOW01_STREAM_TIMESTAMP_INVALID"),
        (-1, "UNAVAILABLE", "SHADOW01_STREAM_TIMESTAMP_INVALID"),
        (
            int((NOW - timedelta(seconds=61)).timestamp() * 1000),
            "STALE",
            "SHADOW01_LIVE_QUOTE_STALE",
        ),
        (
            int((NOW + timedelta(seconds=301)).timestamp() * 1000),
            "UNAVAILABLE",
            "SHADOW01_STREAM_TIMESTAMP_INVALID",
        ),
    ),
)
def test_stream_timestamp_contract_fails_closed_or_marks_stale(
    timestamp: object,
    quality: str,
    reason_code: str,
) -> None:
    quote = _quote(timestamp_milliseconds=timestamp)

    assert quote.quality == quality
    assert quote.reason_codes == (reason_code,)


def test_stream_timestamp_parser_is_distinct_from_rest_epoch_parser() -> None:
    seconds_epoch = int((NOW - timedelta(seconds=30)).timestamp())

    assert smoke_module._quote_timestamp_parse_status(seconds_epoch)[0] == "SECONDS_EPOCH"
    assert stream_timestamp_status(seconds_epoch, NOW, 60) == "SCHEMA_UNSUPPORTED"
    assert stream_timestamp_status(seconds_epoch * 1000, NOW, 60) == "FRESH"
