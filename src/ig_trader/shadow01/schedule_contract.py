"""Value-safe shape evidence for one IG V2 or V3 market-schedule response.

This module never returns a broker body or any source value.  It reduces a
successful JSON object to the small, reviewed shape contract required to
distinguish an absent declared-hours field from a parser problem.
"""

from __future__ import annotations

from collections.abc import Mapping

_CONTRACT_VERSION_PREFIX = "shadow01-v"
_SAFE_TOP_LEVEL_KEY_NAMES = frozenset({"dealingRules", "instrument", "snapshot"})
_SAFE_INSTRUMENT_KEY_NAMES = frozenset(
    {
        "chartCode",
        "contractSize",
        "country",
        "currencies",
        "epic",
        "expiry",
        "forceOpenAllowed",
        "limitedRiskPremium",
        "lotSize",
        "marginDepositBands",
        "marginFactor",
        "marginFactorUnit",
        "marketId",
        "marketStatus",
        "marketTime",
        "name",
        "newsCode",
        "onePipMeans",
        "openingHours",
        "rolloverDetails",
        "slippageFactor",
        "sprintMarketsMaximumExpiryTime",
        "sprintMarketsMinimumExpiryTime",
        "stopsLimitsAllowed",
        "streamingPricesAvailable",
        "type",
        "valueOfOnePip",
    }
)


def v3_schedule_response_contract(
    *,
    response_status: int | None,
    dispatched_version: str | None,
    document: object,
) -> dict[str, object]:
    """Return only approved V3 schedule shape facts, never response values."""

    return _schedule_response_contract(
        response_status=response_status,
        dispatched_version=dispatched_version,
        document=document,
        expected_version="3",
    )


def v2_schedule_response_contract(
    *,
    response_status: int | None,
    dispatched_version: str | None,
    document: object,
) -> dict[str, object]:
    """Return only approved V2 schedule shape facts, never response values."""

    return _schedule_response_contract(
        response_status=response_status,
        dispatched_version=dispatched_version,
        document=document,
        expected_version="2",
    )


def _schedule_response_contract(
    *,
    response_status: int | None,
    dispatched_version: str | None,
    document: object,
    expected_version: str,
) -> dict[str, object]:
    """Reduce one explicitly versioned response to its approved shape only."""

    root = document if isinstance(document, Mapping) else None
    instrument = root.get("instrument") if root is not None else None
    top_level_key_names, top_level_unknown_key_count = _safe_key_summary(
        root,
        allowed=_SAFE_TOP_LEVEL_KEY_NAMES,
    )
    instrument_key_names, instrument_unknown_key_count = _safe_key_summary(
        instrument,
        allowed=_SAFE_INSTRUMENT_KEY_NAMES,
    )
    opening_hours = instrument.get("openingHours") if isinstance(instrument, Mapping) else None
    opening_hours_present = isinstance(instrument, Mapping) and "openingHours" in instrument
    market_times = opening_hours.get("marketTimes") if isinstance(opening_hours, Mapping) else None
    market_times_present = isinstance(opening_hours, Mapping) and "marketTimes" in opening_hours

    return {
        "contract_version": _contract_version(expected_version),
        "response_status": _safe_status(response_status),
        "http_client_dispatch_VERSION": _safe_version(dispatched_version),
        "document_is_object": root is not None,
        "top_level_key_names": top_level_key_names,
        "top_level_unknown_key_count": top_level_unknown_key_count,
        "instrument": {
            "present": root is not None and "instrument" in root,
            "type": _json_type(instrument) if root is not None and "instrument" in root else None,
            "key_names": instrument_key_names,
            "unknown_key_count": instrument_unknown_key_count,
        },
        "openingHours": {
            "present": opening_hours_present,
            "type": _json_type(opening_hours) if opening_hours_present else None,
        },
        "marketTimes": {
            "present": market_times_present,
            "type": _json_type(market_times) if market_times_present else None,
            "count": len(market_times) if isinstance(market_times, list) else None,
        },
        "openTime": _field_shape(market_times, "openTime"),
        "closeTime": _field_shape(market_times, "closeTime"),
    }


def documented_v2_schedule_contract() -> dict[str, object]:
    """Return the offline IG Labs V2 contract reference without a live read.

    This is intentionally documentation-only comparison evidence. It does not
    authorize, construct, or provide a V2 transport path.
    """

    return {
        "comparison_scope": "OFFLINE_DOCUMENTATION_ONLY",
        "live_v2_request_performed": False,
        "endpoint": "GET /markets/{epic}",
        "request_VERSION": "2",
        "instrument.openingHours": {"documented_type": "object"},
        "instrument.openingHours.marketTimes": {"documented_type": "array"},
        "marketTimes.openTime": {"documented_type": "string"},
        "marketTimes.closeTime": {"documented_type": "string"},
    }


def sanitize_v3_schedule_response_contract(value: object) -> dict[str, object] | None:
    """Rebuild a defensive copy of an approved contract-shaped mapping only."""

    return _sanitize_schedule_response_contract(value, expected_version="3")


def sanitize_v2_schedule_response_contract(value: object) -> dict[str, object] | None:
    """Rebuild a defensive copy of an approved V2 contract-shaped mapping only."""

    return _sanitize_schedule_response_contract(value, expected_version="2")


def _sanitize_schedule_response_contract(
    value: object, *, expected_version: str
) -> dict[str, object] | None:
    """Validate one version-pinned shape contract before it leaves the boundary."""

    if not isinstance(value, Mapping):
        return None
    response_status = value.get("response_status")
    dispatched_version = value.get("http_client_dispatch_VERSION")
    document_is_object = value.get("document_is_object")
    top_level_keys = value.get("top_level_key_names")
    top_level_unknown_key_count = value.get("top_level_unknown_key_count")
    instrument = value.get("instrument")
    opening_hours = value.get("openingHours")
    market_times = value.get("marketTimes")
    open_time = value.get("openTime")
    close_time = value.get("closeTime")
    if (
        value.get("contract_version") != _contract_version(expected_version)
        or _safe_status(response_status) != response_status
        or _safe_version(dispatched_version) != dispatched_version
        or not isinstance(document_is_object, bool)
        or not _is_safe_key_list(top_level_keys, allowed=_SAFE_TOP_LEVEL_KEY_NAMES)
        or not _is_nonnegative_int(top_level_unknown_key_count)
        or not _is_presence_type_block(instrument, key_names=True)
        or not _is_presence_type_block(opening_hours)
        or not _is_market_times_block(market_times)
        or not _is_presence_type_block(open_time)
        or not _is_presence_type_block(close_time)
    ):
        return None
    assert isinstance(top_level_keys, list)
    assert isinstance(instrument, Mapping)
    assert isinstance(opening_hours, Mapping)
    assert isinstance(market_times, Mapping)
    assert isinstance(open_time, Mapping)
    assert isinstance(close_time, Mapping)
    return {
        "contract_version": _contract_version(expected_version),
        "response_status": response_status,
        "http_client_dispatch_VERSION": dispatched_version,
        "document_is_object": document_is_object,
        "top_level_key_names": list(top_level_keys),
        "top_level_unknown_key_count": top_level_unknown_key_count,
        "instrument": {
            "present": instrument["present"],
            "type": instrument["type"],
            "key_names": list(instrument["key_names"]),
            "unknown_key_count": instrument["unknown_key_count"],
        },
        "openingHours": {
            "present": opening_hours["present"],
            "type": opening_hours["type"],
        },
        "marketTimes": {
            "present": market_times["present"],
            "type": market_times["type"],
            "count": market_times["count"],
        },
        "openTime": {
            "present": open_time["present"],
            "type": open_time["type"],
        },
        "closeTime": {
            "present": close_time["present"],
            "type": close_time["type"],
        },
    }


def _safe_status(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
        return None
    return value


def _contract_version(expected_version: str) -> str:
    if expected_version not in {"2", "3"}:
        raise ValueError("Shadow01 schedule contract version is invalid")
    return f"{_CONTRACT_VERSION_PREFIX}{expected_version}-schedule-contract/1"


def _safe_version(value: object) -> str | None:
    return value if value in {"1", "2", "3", "4"} else None


def _safe_key_summary(
    value: object,
    *,
    allowed: frozenset[str],
) -> tuple[list[str], int]:
    if not isinstance(value, Mapping):
        return [], 0
    key_names: set[str] = set()
    unknown_key_count = 0
    for key in value:
        if isinstance(key, str) and key in allowed:
            key_names.add(key)
        else:
            unknown_key_count += 1
    return sorted(key_names), unknown_key_count


def _json_type(value: object) -> str:
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "other"


def _field_shape(market_times: object, name: str) -> dict[str, bool | str | None]:
    if not isinstance(market_times, list):
        return {"present": False, "type": None}
    first_row = next((entry for entry in market_times if isinstance(entry, Mapping)), None)
    if first_row is None or name not in first_row:
        return {"present": False, "type": None}
    return {"present": True, "type": _json_type(first_row[name])}


def _is_safe_key_list(value: object, *, allowed: frozenset[str]) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and item in allowed for item in value
    )


def _is_presence_type_block(value: object, *, key_names: bool = False) -> bool:
    if not isinstance(value, Mapping) or not isinstance(value.get("present"), bool):
        return False
    expected = {"present", "type"}
    if key_names:
        expected.update({"key_names", "unknown_key_count"})
    if set(value) != expected:
        return False
    kind = value.get("type")
    allowed_types = {
        "object",
        "array",
        "string",
        "null",
        "boolean",
        "number",
        "other",
        "mixed",
    }
    if kind is not None and kind not in allowed_types:
        return False
    return not key_names or (
        _is_safe_key_list(value.get("key_names"), allowed=_SAFE_INSTRUMENT_KEY_NAMES)
        and _is_nonnegative_int(value.get("unknown_key_count"))
    )


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_market_times_block(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"present", "type", "count"}:
        return False
    if not isinstance(value.get("present"), bool):
        return False
    kind = value.get("type")
    count = value.get("count")
    return (
        kind is None or kind in {"object", "array", "string", "null", "boolean", "number", "other"}
    ) and (count is None or (isinstance(count, int) and not isinstance(count, bool) and count >= 0))


__all__ = (
    "documented_v2_schedule_contract",
    "sanitize_v2_schedule_response_contract",
    "sanitize_v3_schedule_response_contract",
    "v2_schedule_response_contract",
    "v3_schedule_response_contract",
)
