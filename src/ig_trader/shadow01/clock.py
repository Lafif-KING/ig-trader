"""DST-aware completed-session clock rules for SHADOW01."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.ig_trader.shadow01.config import ShadowTournamentConfig

try:
    NEW_YORK: ZoneInfo | None = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    # Some minimal Windows Python installations do not ship the IANA zone
    # database.  The fallback below implements the current US Eastern DST rule
    # deterministically so a missing optional tzdata package cannot turn a
    # safe refusal into a process-import crash.
    NEW_YORK = None
ANCHOR_TIME = time(17, 10)
FX_SESSION_BOUNDARY_TIME = time(17, 0)
_FALLBACK_RULE_FIRST_YEAR = 2007


class ShadowClockError(ValueError):
    """The universal daily clock lacks the requested evidence or is malformed."""


@dataclass(frozen=True)
class ClockAvailability:
    """Read-only provider evidence for one asset class at the proposed anchor."""

    asset_class: str
    read_available: bool
    completed_session_confirmed: bool
    detail: str | None = None


def require_decision_anchor(config: ShadowTournamentConfig, timestamp: datetime) -> datetime:
    """Require precisely 17:10 New York, including correct daylight-saving conversion."""

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ShadowClockError("SHADOW01_DECISION_TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    clock = config.decision_clock
    if clock.get("timezone") != "America/New_York" or clock.get("local_time") != "17:10":
        raise ShadowClockError("SHADOW01_SESSION_CLOCK_HUMAN_GATE_REQUIRED")
    local = _new_york_local(timestamp)
    if local.time().replace(tzinfo=None) != ANCHOR_TIME:
        raise ShadowClockError("SHADOW01_DECISION_TIMESTAMP_IS_NOT_THE_FROZEN_ANCHOR")
    return timestamp.astimezone(UTC)


def new_york_local_date(timestamp: datetime) -> date:
    """Return the New York calendar date for one aware instant.

    The monitor uses this only to decide whether a late wake belongs to the
    current New York market day.  Keeping the conversion here makes its DST
    behavior identical to :func:`require_decision_anchor` rather than relying
    on a second, host-dependent timezone calculation in the scheduler.
    """

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ShadowClockError("SHADOW01_DECISION_TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    return _new_york_local(timestamp).date()


def decision_anchor_for_date(config: ShadowTournamentConfig, local_date: date) -> datetime:
    """Return the frozen 17:10 New York anchor for one calendar day in UTC.

    The anchor is deliberately generated from a New York calendar date rather
    than by adding twenty-four UTC hours.  This preserves the exact local
    anchor through both DST transitions and gives every restart the same
    canonical session key.
    """

    if not isinstance(local_date, date):
        raise ShadowClockError("SHADOW01_DECISION_DATE_INVALID")
    clock = config.decision_clock
    if clock.get("timezone") != "America/New_York" or clock.get("local_time") != "17:10":
        raise ShadowClockError("SHADOW01_SESSION_CLOCK_HUMAN_GATE_REQUIRED")
    anchor = _new_york_wall_time_to_utc(local_date, ANCHOR_TIME)
    return require_decision_anchor(config, anchor)


def fx_session_boundary_for_date(local_date: date) -> datetime:
    """Return the fixed 17:00 New York FX daily boundary for one local date."""

    if not isinstance(local_date, date):
        raise ShadowClockError("SHADOW01_DECISION_DATE_INVALID")
    return _new_york_wall_time_to_utc(local_date, FX_SESSION_BOUNDARY_TIME)


def fx_anchor_follows_completed_session(config: ShadowTournamentConfig, local_date: date) -> bool:
    """Prove the frozen FX observation anchor is after its 17:00 NY boundary."""

    return decision_anchor_for_date(config, local_date) > fx_session_boundary_for_date(local_date)


def decision_session_key(
    config: ShadowTournamentConfig,
    *,
    instrument: str,
    decision_timestamp_utc: datetime,
) -> str:
    """Return the immutable version/instrument/completed-session key.

    No provider value is part of this key.  Its only time component is the
    validated frozen anchor, so manual reruns and restarts resolve to exactly
    the same logical observation rather than a new timestamp.
    """

    if not isinstance(instrument, str) or not instrument or instrument != instrument.strip():
        raise ShadowClockError("SHADOW01_DECISION_INSTRUMENT_INVALID")
    anchor = require_decision_anchor(config, decision_timestamp_utc)
    return f"{config.version}:{instrument}:{anchor.isoformat()}"


def _new_york_local(timestamp: datetime) -> datetime:
    """Convert an aware instant to New York, with a deterministic tzdata fallback.

    The fallback is deliberately limited to the modern US DST rules (in force
    since 2007), which cover the tournament's prospective dates.  Earlier
    dates fail closed rather than being evaluated under an incorrect rule.  It
    is used only to evaluate the fixed clock, never to infer market-session
    metadata.
    """

    if NEW_YORK is not None:
        return timestamp.astimezone(NEW_YORK)
    instant = timestamp.astimezone(UTC)
    if instant.year < _FALLBACK_RULE_FIRST_YEAR:
        raise ShadowClockError("SHADOW01_TIMEZONE_DATA_REQUIRED")
    start = _us_eastern_dst_start_utc(instant.year)
    end = _us_eastern_dst_end_utc(instant.year)
    offset_hours = -4 if start <= instant < end else -5
    return instant.astimezone(timezone(timedelta(hours=offset_hours), "America/New_York"))


def _new_york_wall_time_to_utc(local_date: date, local_time: time) -> datetime:
    """Convert an unambiguous afternoon New York wall time to UTC safely."""

    if NEW_YORK is not None:
        return datetime.combine(local_date, local_time, tzinfo=NEW_YORK).astimezone(UTC)
    if local_date.year < _FALLBACK_RULE_FIRST_YEAR:
        raise ShadowClockError("SHADOW01_TIMEZONE_DATA_REQUIRED")
    local_as_utc = datetime.combine(local_date, local_time, tzinfo=UTC)
    dst_candidate = local_as_utc + timedelta(hours=4)
    if (
        _us_eastern_dst_start_utc(local_date.year)
        <= dst_candidate
        < _us_eastern_dst_end_utc(local_date.year)
    ):
        return dst_candidate
    return local_as_utc + timedelta(hours=5)


def _us_eastern_dst_start_utc(year: int) -> datetime:
    """Second Sunday in March at 07:00 UTC under the post-2007 US rule."""

    march_first = datetime(year, 3, 1, tzinfo=UTC)
    first_sunday = 1 + (6 - march_first.weekday()) % 7
    return datetime(year, 3, first_sunday + 7, 7, tzinfo=UTC)


def _us_eastern_dst_end_utc(year: int) -> datetime:
    """First Sunday in November at 06:00 UTC under the post-2007 US rule."""

    november_first = datetime(year, 11, 1, tzinfo=UTC)
    first_sunday = 1 + (6 - november_first.weekday()) % 7
    return datetime(year, 11, first_sunday, 6, tzinfo=UTC)


def assess_universal_clock(
    availability: tuple[ClockAvailability, ...],
) -> tuple[str, tuple[str, ...]]:
    """Refuse a guessed asset-class override when the universal anchor is unproven."""

    expected = {"FX", "METAL", "INDEX"}
    indexed = {item.asset_class: item for item in availability}
    missing = sorted(expected - set(indexed))
    failures = [
        item.asset_class
        for item in availability
        if item.asset_class in expected
        and (not item.read_available or not item.completed_session_confirmed)
    ]
    if missing or failures:
        return "SHADOW01_SESSION_CLOCK_HUMAN_GATE_REQUIRED", tuple(sorted((*missing, *failures)))
    return "SHADOW01_SESSION_CLOCK_VERIFIED", ()


__all__ = (
    "ANCHOR_TIME",
    "FX_SESSION_BOUNDARY_TIME",
    "ClockAvailability",
    "ShadowClockError",
    "assess_universal_clock",
    "decision_anchor_for_date",
    "decision_session_key",
    "fx_anchor_follows_completed_session",
    "fx_session_boundary_for_date",
    "new_york_local_date",
    "require_decision_anchor",
)
