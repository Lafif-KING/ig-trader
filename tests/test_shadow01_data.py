"""Focused IG V2 DAY-history contract tests for the Shadow01 parser."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.ig_trader.shadow01.data import (
    history_row_contract_diagnostic,
    parse_completed_daily_bars,
)


def _row(timestamp: str) -> dict[str, object]:
    quote = {"bid": "1.0000", "offer": "1.0002"}
    return {
        "snapshotTimeUTC": timestamp,
        "openPrice": quote,
        "highPrice": quote,
        "lowPrice": quote,
        "closePrice": quote,
    }


def test_v2_day_history_accepts_naive_utc_rows_and_excludes_only_current_day_candle() -> None:
    observed_at = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    current_day = datetime(2026, 9, 2, tzinfo=UTC)
    rows = [
        _row((current_day - timedelta(days=299 - index)).strftime("%Y-%m-%dT%H:%M:%S"))
        for index in range(300)
    ]

    completed = parse_completed_daily_bars({"prices": rows}, decision_timestamp_utc=observed_at)

    assert len(rows) == 300
    assert len(completed) == 299
    assert all(bar.completed_at < current_day for bar in completed)
    assert completed[-1].completed_at == current_day - timedelta(days=1)
    assert len(completed) >= 273
    assert len(completed) >= 61


def test_history_contract_diagnostic_reports_only_shape_and_types() -> None:
    row = _row("2026-09-01T00:00:00")

    diagnostic = history_row_contract_diagnostic(row)

    assert diagnostic == {
        "row_type": "dict",
        "top_level_row_keys": [
            "closePrice",
            "highPrice",
            "lowPrice",
            "openPrice",
            "snapshotTimeUTC",
        ],
        "timestamp_like_field_names": ["snapshotTimeUTC"],
        "snapshotTime": {"present": False, "type": None, "format": None},
        "snapshotTimeUTC": {"present": True, "type": "str", "format": "ISO8601_NAIVE"},
        "ohlc": {
            "openPrice": {
                "present": True,
                "type": "dict",
                "keys": ["bid", "offer"],
                "key_types": {"bid": "str", "offer": "str"},
            },
            "highPrice": {
                "present": True,
                "type": "dict",
                "keys": ["bid", "offer"],
                "key_types": {"bid": "str", "offer": "str"},
            },
            "lowPrice": {
                "present": True,
                "type": "dict",
                "keys": ["bid", "offer"],
                "key_types": {"bid": "str", "offer": "str"},
            },
            "closePrice": {
                "present": True,
                "type": "dict",
                "keys": ["bid", "offer"],
                "key_types": {"bid": "str", "offer": "str"},
            },
        },
        "lastTradedVolume": {"present": False, "type": None},
    }
    assert "1.0000" not in str(diagnostic)
    assert "2026-09-01" not in str(diagnostic)


def test_history_contract_diagnostic_classifies_ig_slash_timestamp_without_its_value() -> None:
    row = _row("2026-09-01T00:00:00")
    row.pop("snapshotTimeUTC")
    row["snapshotTime"] = "2026/09/01 00:00:00"
    row["lastTradedVolume"] = "10"

    diagnostic = history_row_contract_diagnostic(row)

    assert diagnostic["snapshotTime"] == {
        "present": True,
        "type": "str",
        "format": "IG_SLASH_DATETIME",
    }
    assert diagnostic["snapshotTimeUTC"] == {"present": False, "type": None, "format": None}
    assert diagnostic["timestamp_like_field_names"] == ["snapshotTime"]
    assert diagnostic["lastTradedVolume"] == {"present": True, "type": "str"}
    assert "2026/09/01" not in str(diagnostic)


def test_v2_day_history_accepts_the_live_proven_snapshot_time_representation() -> None:
    observed_at = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    current_day = datetime(2026, 9, 2, tzinfo=UTC)
    rows = []
    for index in range(300):
        row = _row((current_day - timedelta(days=299 - index)).strftime("%Y-%m-%dT%H:%M:%S"))
        row["snapshotTime"] = (current_day - timedelta(days=299 - index)).strftime(
            "%Y/%m/%d %H:%M:%S"
        )
        row.pop("snapshotTimeUTC")
        rows.append(row)

    completed = parse_completed_daily_bars({"prices": rows}, decision_timestamp_utc=observed_at)

    assert len(completed) == 299
    assert completed[-1].completed_at == current_day - timedelta(days=1)
