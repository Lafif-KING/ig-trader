"""Small immutable SL-02 research contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from src.ig_trader.strategy_lab.data import CanonicalDataset
from src.ig_trader.strategy_lab.models import Timeframe


class AlignmentStatus(StrEnum):
    ALIGNED_WITH_IG = "ALIGNED_WITH_IG"
    ACCEPTABLE_SOURCE_DIFFERENCE = "ACCEPTABLE_SOURCE_DIFFERENCE"
    MATERIAL_SOURCE_DIVERGENCE = "MATERIAL_SOURCE_DIVERGENCE"
    NO_OVERLAP_AVAILABLE = "NO_OVERLAP_AVAILABLE"


class DatasetDepthStatus(StrEnum):
    SUFFICIENT = "SUFFICIENT"
    LOW_DATA_DEPTH = "LOW_DATA_DEPTH"


@dataclass(frozen=True)
class BrokerValidationPoint:
    timestamp_utc: datetime
    close_mid: Decimal
    spread: Decimal | None


@dataclass(frozen=True)
class BrokerEvidence:
    """Sanitized DQ-03 facts. Missing values are deliberately represented as None."""

    symbol: str
    epic: str | None
    metadata_fingerprint: str | None
    broker_validation_fingerprint: str | None
    data_status: str | None
    cost_model_status: str | None
    pip_or_tick_size: Decimal | None
    minimum_deal_size: Decimal | None
    minimum_stop_distance: Decimal | None
    observed_spread: Decimal | None
    currency: str | None
    points: tuple[BrokerValidationPoint, ...] = ()


@dataclass(frozen=True)
class CostEvidence:
    """Reviewed research friction inputs tied to a specific DQ-03 metadata fingerprint."""

    symbol: str
    metadata_fingerprint: str
    base_spread: Decimal
    slippage: Decimal
    commission_price_equivalent: Decimal
    allowed_utc_hours: frozenset[int]
    evidence_basis: str


@dataclass(frozen=True)
class AlignmentResult:
    status: AlignmentStatus
    overlapping_rows: int
    timestamp_alignment_rate: Decimal | None
    average_absolute_price_difference: Decimal | None
    average_spread_difference: Decimal | None
    missing_candle_rate: Decimal | None
    reason: str


@dataclass(frozen=True)
class AcquiredDataset:
    dataset: CanonicalDataset
    provider: str
    provider_symbol: str
    acquisition_timestamp_utc: datetime
    source_url: str
    raw_source_fingerprint: str
    cached: bool
    depth_status: DatasetDepthStatus

    @property
    def timeframe(self) -> Timeframe:
        return self.dataset.timeframe
