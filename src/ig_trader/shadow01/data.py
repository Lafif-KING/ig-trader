"""Strict parsing and caching for read-only, completed IG daily observations."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, time
from math import isfinite
from pathlib import Path
from typing import Any

from src.ig_trader.shadow01.models import DailyBar, fingerprint


class ShadowDataError(ValueError):
    """Read-only market data is missing, incomplete, or cannot be attributed safely."""


def parse_completed_daily_bars(
    document: Mapping[str, object], *, decision_timestamp_utc: datetime
) -> tuple[DailyBar, ...]:
    """Parse only strictly earlier daily observations; never retain a current bar."""

    rows = document.get("prices")
    if not isinstance(rows, list):
        raise ShadowDataError("SHADOW01_HISTORY_RESPONSE_INVALID")
    decision_time = _utc(decision_timestamp_utc)
    bars: list[DailyBar] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        bar = _bar(row)
        if bar is not None and _daily_session_completed(bar.completed_at, decision_time):
            bars.append(bar)
    ordered = tuple(sorted(bars, key=lambda item: item.completed_at))
    if len({item.completed_at for item in ordered}) != len(ordered):
        raise ShadowDataError("SHADOW01_HISTORY_HAS_DUPLICATE_SESSIONS")
    if not ordered:
        raise ShadowDataError("SHADOW01_NO_COMPLETED_HISTORY")
    return ordered


def cache_history(path: Path, *, epic: str, document: Mapping[str, object]) -> str:
    """Cache one validated raw response locally; callers decide when a refresh is needed."""

    if not epic.strip():
        raise ShadowDataError("SHADOW01_EPIC_INVALID")
    payload = {
        "epic": epic,
        "history": dict(document),
        "input_data_fingerprint": fingerprint(document),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return str(payload["input_data_fingerprint"])


def load_cached_history(path: Path, *, epic: str) -> Mapping[str, object] | None:
    """Load only a cache attributable to the same explicitly verified EPIC."""

    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("epic") != epic:
        return None
    history = payload.get("history")
    return history if isinstance(history, Mapping) else None


def _bar(row: Mapping[str, object]) -> DailyBar | None:
    timestamp = _timestamp(row)
    open_price = _mid(_mapping(row.get("openPrice")))
    high_price = _mid(_mapping(row.get("highPrice")))
    low_price = _mid(_mapping(row.get("lowPrice")))
    close_block = _mapping(row.get("closePrice"))
    close_price = _mid(close_block)
    if None in {timestamp, open_price, high_price, low_price, close_price}:
        return None
    bid = _number(close_block.get("bid"))
    offer = _number(close_block.get("offer")) or _number(close_block.get("ask"))
    try:
        return DailyBar(
            completed_at=timestamp,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            bid=bid,
            offer=offer,
        )
    except ValueError:
        return None


def _timestamp(row: Mapping[str, object]) -> datetime | None:
    """Parse only observed IG V2 DAY timestamp representations.

    ``snapshotTimeUTC`` is accepted only as ISO 8601, with an offset-free
    value interpreted as UTC for that explicitly named field.  Gate 09's
    bounded Demo proof established a second representation: ``snapshotTime``
    as an exact ``YYYY/MM/DD HH:MM:SS`` UTC DAY-bar value.  No other field or
    timestamp representation is treated as a fallback.
    """

    value = row.get("snapshotTimeUTC")
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    snapshot_time = row.get("snapshotTime")
    if not isinstance(snapshot_time, str):
        return None
    try:
        return datetime.strptime(snapshot_time, "%Y/%m/%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _mid(value: Mapping[str, object]) -> float | None:
    bid = _number(value.get("bid"))
    offer = _number(value.get("offer")) or _number(value.get("ask"))
    if bid is not None and offer is not None and offer >= bid:
        return (bid + offer) / 2
    return _number(value.get("lastTraded"))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) and parsed > 0 else None


def history_row_contract_diagnostic(row: object) -> dict[str, object]:
    """Describe an IG V2 history row's shape without retaining values."""

    value = _mapping(row)
    return {
        "row_type": type(row).__name__,
        "top_level_row_keys": _safe_key_names(value),
        "timestamp_like_field_names": _timestamp_like_field_names(value),
        "snapshotTime": _timestamp_field_contract(value, "snapshotTime"),
        "snapshotTimeUTC": _timestamp_field_contract(value, "snapshotTimeUTC"),
        "ohlc": {
            "openPrice": _price_block_contract(value.get("openPrice")),
            "highPrice": _price_block_contract(value.get("highPrice")),
            "lowPrice": _price_block_contract(value.get("lowPrice")),
            "closePrice": _price_block_contract(value.get("closePrice")),
        },
        "lastTradedVolume": {
            "present": "lastTradedVolume" in value,
            "type": _value_type(value.get("lastTradedVolume")),
        },
    }


def _daily_session_completed(timestamp: datetime, decision_time: datetime) -> bool:
    """Exclude only the known current UTC-midnight DAY candle representation."""

    if timestamp >= decision_time:
        return False
    is_current_utc_midnight = (
        timestamp.date() == decision_time.date()
        and timestamp.timetz().replace(tzinfo=None) == time()
    )
    return not is_current_utc_midnight


def _price_block_contract(value: object) -> dict[str, object]:
    block = _mapping(value)
    return {
        "present": value is not None,
        "type": type(value).__name__ if value is not None else None,
        "keys": _safe_key_names(block),
        "key_types": {key: _value_type(block[key]) for key in _safe_key_names(block)},
    }


def _timestamp_like_field_names(value: Mapping[str, object]) -> list[str]:
    """Return safe field names that may carry a timestamp, never their values."""

    return sorted(
        key
        for key in _safe_key_names(value)
        if "time" in key.casefold() or "date" in key.casefold()
    )


def _timestamp_field_contract(value: Mapping[str, object], field_name: str) -> dict[str, object]:
    timestamp = value.get(field_name)
    return {
        "present": field_name in value,
        "type": _value_type(timestamp),
        "format": _timestamp_format(timestamp),
    }


def _timestamp_format(value: object) -> str | None:
    """Classify a history timestamp without serialising its contents."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return "EMPTY_STRING"
    if re.fullmatch(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", text):
        return "IG_SLASH_DATETIME"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return "STRING_UNRECOGNIZED"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return "ISO8601_NAIVE"
    return "ISO8601_OFFSET"


def _safe_key_names(value: Mapping[str, object]) -> list[str]:
    return sorted(key for key in value if isinstance(key, str) and key.isidentifier())


def _value_type(value: object) -> str | None:
    return type(value).__name__ if value is not None else None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ShadowDataError("SHADOW01_DECISION_TIMESTAMP_INVALID")
    return value.astimezone(UTC)


__all__ = (
    "ShadowDataError",
    "cache_history",
    "history_row_contract_diagnostic",
    "load_cached_history",
    "parse_completed_daily_bars",
)
