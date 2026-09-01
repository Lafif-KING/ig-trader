"""Small immutable contracts for the observation-only Shadow Tournament."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum

from src.ig_trader.shadow01.config import EXPECTED_SYMBOLS


class AssetClass(StrEnum):
    FX = "FX"
    METAL = "METAL"
    INDEX = "INDEX"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"
    BLOCK = "BLOCK"


class PolicyId(StrEnum):
    P0_TECHNICAL_TREND_ONLY = "P0_TECHNICAL_TREND_ONLY"
    P1_TECHNICAL_REVERSION_ONLY = "P1_TECHNICAL_REVERSION_ONLY"
    P2_TREND_PLUS_CROSS_ASSET = "P2_TREND_PLUS_CROSS_ASSET"
    P3_CONSERVATIVE_CONTEXT = "P3_CONSERVATIVE_CONTEXT"


class QualityState(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class CostState(StrEnum):
    COST_OK = "COST_OK"
    COST_HIGH = "COST_HIGH"
    COST_UNKNOWN = "COST_UNKNOWN"


class ContextState(StrEnum):
    SUPPORTIVE = "SUPPORTIVE"
    NEUTRAL = "NEUTRAL"
    OPPOSES = "OPPOSES"
    UNKNOWN = "UNKNOWN"


class FundamentalState(StrEnum):
    NORMAL = "NORMAL"
    EVENT_RISK = "EVENT_RISK"
    UNKNOWN = "UNKNOWN"


class MarketDataState(StrEnum):
    AVAILABLE = "AVAILABLE"
    MARKET_DATA_UNAVAILABLE = "MARKET_DATA_UNAVAILABLE"


@dataclass(frozen=True)
class MarketSpec:
    """A DQ-03-proven contract or an explicit unavailable placeholder."""

    symbol: str
    asset_class: AssetClass
    epic: str | None
    state: MarketDataState
    reason: str | None = None
    metadata: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.symbol.isascii() or not self.symbol.isupper() or not self.symbol.isalnum():
            raise ValueError("Shadow market symbol is invalid")
        if self.state is MarketDataState.AVAILABLE and not self.epic:
            raise ValueError("An available Shadow market requires a verified EPIC")
        if self.state is MarketDataState.MARKET_DATA_UNAVAILABLE and self.epic is not None:
            raise ValueError("An unavailable Shadow market must not retain an assumed EPIC")


@dataclass(frozen=True)
class DailyBar:
    """One completed market-day observation, never an in-progress candle."""

    completed_at: datetime
    open: float
    high: float
    low: float
    close: float
    bid: float | None = None
    offer: float | None = None

    def __post_init__(self) -> None:
        timestamp = require_utc(self.completed_at)
        object.__setattr__(self, "completed_at", timestamp)
        values = (self.open, self.high, self.low, self.close)
        if not all(is_finite_positive(value) for value in values):
            raise ValueError("Shadow daily-bar price is invalid")
        if not self.low <= self.open <= self.high or not self.low <= self.close <= self.high:
            raise ValueError("Shadow daily-bar OHLC geometry is invalid")
        if self.bid is not None and not is_finite_positive(self.bid):
            raise ValueError("Shadow daily-bar bid is invalid")
        if self.offer is not None and not is_finite_positive(self.offer):
            raise ValueError("Shadow daily-bar offer is invalid")
        if self.bid is not None and self.offer is not None and self.offer < self.bid:
            raise ValueError("Shadow daily-bar spread is invalid")

    @property
    def spread(self) -> float | None:
        return self.offer - self.bid if self.bid is not None and self.offer is not None else None


@dataclass(frozen=True)
class TechnicalState:
    return_1: float | None
    return_5: float | None
    return_20: float | None
    return_60: float | None
    atr20_over_price: float | None
    realized_volatility_20: float | None
    trend_20: float | None
    trend_60: float | None
    trend_strength: float | None
    distance_from_60_session_mean: float | None
    drawdown_from_60_session_high: float | None


@dataclass(frozen=True)
class TechnicalOpinion:
    direction: Direction
    strength: float | None
    normalized_20: float | None
    normalized_60: float | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ReversionOpinion:
    direction: Direction
    percentile: float | None
    normalized_return_5: float | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class CrossAssetOpinion:
    state: ContextState
    score: float | None
    reason_codes: tuple[str, ...]
    fields: dict[str, object]


@dataclass(frozen=True)
class FundamentalContext:
    state: FundamentalState
    policy_state: str | None
    policy_trend: str | None
    data_quality: str | None
    staleness_seconds: int | None
    event_risk: bool | None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CostContext:
    state: CostState
    spread: float | None
    spread_to_atr: float | None
    minimum_stop_distance: float | None
    product_type: str | None
    funding_metadata: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class QualityAssessment:
    state: QualityState
    data_age_seconds: int | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class MarketSnapshot:
    """Causal observation input retained before policy outcomes are known."""

    decision_timestamp_utc: datetime
    instrument: str
    epic: str
    asset_class: AssetClass
    completed_bars: tuple[DailyBar, ...]
    metadata: dict[str, object]
    data_quality: QualityAssessment
    input_data_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_timestamp_utc", require_utc(self.decision_timestamp_utc))
        if not self.instrument or not self.epic or not self.completed_bars:
            raise ValueError("Shadow market snapshot is incomplete")
        timestamps = tuple(bar.completed_at for bar in self.completed_bars)
        if tuple(sorted(timestamps)) != timestamps or len(set(timestamps)) != len(timestamps):
            raise ValueError("Shadow bars must be strictly ordered and unique")
        if self.completed_bars[-1].completed_at >= self.decision_timestamp_utc:
            raise ValueError("Shadow snapshot includes an incomplete or future bar")


@dataclass(frozen=True)
class ShadowDecision:
    """Append-only opinion record. It intentionally has no dealing identifier."""

    decision_id: str
    tournament_version: str
    config_fingerprint: str
    decision_timestamp_utc: datetime
    instrument: str
    epic: str
    policy_id: PolicyId
    direction: Direction
    technical_engine: str
    technical_score: float | None
    cross_asset_state: ContextState
    fundamental_context: FundamentalState
    quality_state: QualityState
    cost_state: CostState
    factor_tags: tuple[str, ...]
    reason_codes: tuple[str, ...]
    input_data_fingerprint: str
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_timestamp_utc", require_utc(self.decision_timestamp_utc))
        object.__setattr__(self, "created_at", require_utc(self.created_at))
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.decision_id,
                self.tournament_version,
                self.config_fingerprint,
                self.instrument,
                self.epic,
                self.technical_engine,
                self.input_data_fingerprint,
            )
        ):
            raise ValueError("Shadow decision identity is incomplete")
        if not all(
            (
                isinstance(self.policy_id, PolicyId),
                isinstance(self.direction, Direction),
                isinstance(self.cross_asset_state, ContextState),
                isinstance(self.fundamental_context, FundamentalState),
                isinstance(self.quality_state, QualityState),
                isinstance(self.cost_state, CostState),
            )
        ):
            raise ValueError("Shadow decision state is invalid")
        if self.technical_score is not None and (
            isinstance(self.technical_score, bool)
            or not isinstance(self.technical_score, (int, float))
            or not math.isfinite(self.technical_score)
        ):
            raise ValueError("Shadow decision technical score is invalid")
        if not isinstance(self.factor_tags, tuple) or not all(
            isinstance(tag, str) and tag.strip() for tag in self.factor_tags
        ):
            raise ValueError("Shadow decision factor tags are invalid")
        if len(set(self.factor_tags)) != len(self.factor_tags):
            raise ValueError("Shadow decision factor tags must be unique")
        if self.factor_tags != expected_factor_tags(self.instrument, self.direction):
            raise ValueError("Shadow decision factor tags do not match the frozen scope")
        if (
            not isinstance(self.reason_codes, tuple)
            or not self.reason_codes
            or not all(isinstance(code, str) and code.strip() for code in self.reason_codes)
        ):
            raise ValueError("Shadow decision reason codes are invalid")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("Shadow decision reason codes must be unique")
        _validate_directional_decision_gates(self)

    def document(self) -> dict[str, object]:
        return document(self)


@dataclass(frozen=True)
class OutcomeLabel:
    """A later, separate label for one directional Shadow decision."""

    decision_id: str
    horizon_sessions: int
    reference_entry_price: float | None
    future_price: float | None
    raw_directional_return: float | None
    atr_normalized_return: float | None
    cost_adjusted_result: float | None
    outcome_timestamp_utc: datetime | None
    quality: QualityState
    blocked_reason: str | None

    def __post_init__(self) -> None:
        if self.horizon_sessions not in {1, 3, 5, 10, 20}:
            raise ValueError("Shadow outcome horizon is invalid")
        if not isinstance(self.decision_id, str) or not self.decision_id.strip():
            raise ValueError("Shadow outcome decision identity is invalid")
        if not isinstance(self.quality, QualityState):
            raise ValueError("Shadow outcome quality is invalid")
        if self.outcome_timestamp_utc is not None:
            object.__setattr__(
                self,
                "outcome_timestamp_utc",
                require_utc(self.outcome_timestamp_utc),
            )
        if self.reference_entry_price is not None and not is_finite_positive(
            self.reference_entry_price
        ):
            raise ValueError("Shadow outcome reference entry price is invalid")
        if self.future_price is not None and not is_finite_positive(self.future_price):
            raise ValueError("Shadow outcome future price is invalid")
        for value in (self.raw_directional_return, self.atr_normalized_return):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("Shadow outcome numeric value is invalid")
        if self.cost_adjusted_result is not None:
            raise ValueError("Shadow outcome cost model is not approved")
        if self.quality is QualityState.BLOCKED:
            if not isinstance(self.blocked_reason, str) or not self.blocked_reason.strip():
                raise ValueError("Blocked Shadow outcome requires a reason")
            if any(
                value is not None
                for value in (
                    self.future_price,
                    self.raw_directional_return,
                    self.atr_normalized_return,
                    self.outcome_timestamp_utc,
                )
            ):
                raise ValueError("Blocked Shadow outcome must not contain a resolved result")
        elif self.blocked_reason is not None:
            raise ValueError("Resolved Shadow outcome must not contain a blocked reason")
        elif (
            self.reference_entry_price is None
            or self.future_price is None
            or self.raw_directional_return is None
            or self.outcome_timestamp_utc is None
        ):
            raise ValueError("Resolved Shadow outcome is incomplete")


def require_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Shadow timestamp must be timezone-aware")
    return value.astimezone(UTC)


def is_finite_positive(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def expected_factor_tags(instrument: str, direction: Direction) -> tuple[str, ...]:
    """Return the one deterministic, auditable factor expression for a decision."""

    if not isinstance(instrument, str) or instrument not in EXPECTED_SYMBOLS:
        raise ValueError("Shadow decision instrument is outside the frozen scope")
    if not isinstance(direction, Direction):
        raise ValueError("Shadow decision direction is invalid")
    if direction not in {Direction.LONG, Direction.SHORT}:
        return ()
    polarity = "LONG" if direction is Direction.LONG else "SHORT"
    opposite = "SHORT" if polarity == "LONG" else "LONG"
    if len(instrument) == 6 and instrument not in {"XAUUSD", "XAGUSD"}:
        return (f"{instrument[:3]}_{polarity}", f"{instrument[3:]}_{opposite}")
    if instrument == "XAUUSD":
        return (f"GOLD_{polarity}", f"USD_{opposite}")
    if instrument == "XAGUSD":
        return (f"SILVER_{polarity}", f"USD_{opposite}")
    return (
        f"EQUITY_BETA_{polarity}",
        "RISK_ON" if polarity == "LONG" else "RISK_OFF",
    )


def _validate_directional_decision_gates(value: ShadowDecision) -> None:
    """Mirror frozen policy gates at the persistent decision boundary.

    This protects append-only storage if a caller bypasses
    ``policies.materialize_decisions`` and constructs a model directly.
    """

    if value.direction not in {Direction.LONG, Direction.SHORT}:
        return
    if value.quality_state is QualityState.BLOCKED:
        raise ValueError("SHADOW01_DIRECTIONAL_DECISION_QUALITY_BLOCKED")
    if value.policy_id is PolicyId.P2_TREND_PLUS_CROSS_ASSET and value.cross_asset_state not in {
        ContextState.SUPPORTIVE,
        ContextState.NEUTRAL,
    }:
        raise ValueError("SHADOW01_DIRECTIONAL_P2_CONTEXT_BLOCKED")
    if value.policy_id is PolicyId.P3_CONSERVATIVE_CONTEXT and (
        value.cost_state is CostState.COST_HIGH
        or value.cross_asset_state is ContextState.OPPOSES
        or value.fundamental_context is FundamentalState.EVENT_RISK
    ):
        raise ValueError("SHADOW01_DIRECTIONAL_P3_CONTEXT_BLOCKED")


def document(value: object) -> dict[str, object]:
    """Render immutable records as JSON-safe facts without exposing broker credentials."""

    if not hasattr(value, "__dataclass_fields__"):
        raise TypeError("Only Shadow dataclasses may be documented")
    # Python's ``frozen=True`` prevents ordinary assignment but not deliberate
    # ``object.__setattr__`` bypasses. Re-run the two persistence-facing
    # contracts immediately before serializing them for append-only storage.
    if isinstance(value, (ShadowDecision, OutcomeLabel)):
        value.__post_init__()
    rendered = _normalize(asdict(value))
    assert isinstance(rendered, dict)
    return rendered


def fingerprint(value: object) -> str:
    """Canonical identity for raw input facts, never a result-dependent value."""

    canonical = json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize(value: object) -> object:
    if isinstance(value, datetime):
        return require_utc(value).isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return _normalize(asdict(value))
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Shadow document contains a non-finite float")
        return value
    if value is None or isinstance(value, str | int | bool):
        return value
    raise TypeError(f"Unsupported Shadow document value: {type(value)!r}")
