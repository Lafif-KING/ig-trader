"""Synthetic Gate-11 coverage for the narrow V3 schedule-metadata bridge."""

from __future__ import annotations

from datetime import time

from src.ig_trader.shadow01.schedule_metadata import parse_v3_market_schedule


def _v3_document(open_time: object = "00:00", close_time: object = "23:59") -> dict[str, object]:
    return {
        "instrument": {
            "openingHours": {"marketTimes": [{"openTime": open_time, "closeTime": close_time}]}
        },
        # This deliberately irrelevant data proves the parser never turns a
        # V3 document into a canonical price source.
        "snapshot": {"bid": "not-a-shadow-price", "offer": "not-a-shadow-price"},
    }


def test_v3_schedule_bridge_retains_only_declared_hours_contract() -> None:
    evidence = parse_v3_market_schedule(
        symbol="EURUSD", epic="TEST.EURUSD", document=_v3_document()
    )

    assert evidence.source_version == 3
    assert evidence.hours_state == "DECLARED_HOURS_AVAILABLE"
    assert evidence.target_anchor_in_declared_operational_window(time(17, 10)) is True
    document = evidence.document()
    assert document == {
        "symbol": "EURUSD",
        "epic_present": True,
        "source_version": 3,
        "opening_hours_available": True,
        "market_times_available": True,
        "market_times_count": 1,
        "open_time_type": "str",
        "close_time_type": "str",
        "hours_state": "DECLARED_HOURS_AVAILABLE",
    }
    assert "snapshot" not in document
    assert "not-a-shadow-price" not in str(document)


def test_v3_schedule_bridge_fails_closed_for_missing_ambiguous_and_invalid_hours() -> None:
    missing = parse_v3_market_schedule(
        symbol="XAUUSD", epic="TEST.XAUUSD", document={"instrument": {}}
    )
    ambiguous = parse_v3_market_schedule(
        symbol="US500", epic="TEST.US500", document=_v3_document("00:00", "00:00")
    )
    invalid = parse_v3_market_schedule(
        symbol="USTECH100", epic="TEST.USTECH100", document=_v3_document("25:00", "23:59")
    )

    assert missing.hours_state == "DECLARED_HOURS_UNAVAILABLE"
    assert ambiguous.hours_state == "DECLARED_HOURS_AMBIGUOUS"
    assert invalid.hours_state == "DECLARED_HOURS_INVALID"
    assert all(
        item.target_anchor_in_declared_operational_window(time(17, 10)) is None
        for item in (missing, ambiguous, invalid)
    )


def test_v3_schedule_window_never_infers_an_outside_anchor_as_available() -> None:
    evidence = parse_v3_market_schedule(
        symbol="US500", epic="TEST.US500", document=_v3_document("09:30", "16:00")
    )

    assert evidence.hours_state == "DECLARED_HOURS_AVAILABLE"
    assert evidence.target_anchor_in_declared_operational_window(time(17, 10)) is False
