"""Deterministic, gap-safe partitions for offline research datasets.

An unexplained gap is never repaired or ignored here.  It creates a hard
boundary, so every returned segment starts with fresh strategy state and no
simulated trade can cross from one segment to another.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from src.ig_trader.strategy_lab.data import (
    TIMEFRAME_INTERVALS,
    CanonicalDataset,
    DatasetGap,
    GapClassification,
    LabCandle,
    build_dataset,
)
from src.ig_trader.strategy_lab.models import Timeframe

# The frozen engines require 22 candles before the first signal.  Three 100-candle
# chronological portions leave every development/validation/test portion with at
# least 60 candles: 22 warm-up candles and 38 independently evaluated candles.
# This is a fixed data-quality rule, not a profitability-tuned threshold.
MINIMUM_SEGMENT_CANDLES = 300
MINIMUM_PHASE_CANDLES = 60

MINIMUM_DEPTH_DAYS: dict[Timeframe, int] = {
    Timeframe.H4: 365,
    Timeframe.H1: 180,
    Timeframe.M15: 90,
    Timeframe.M5: 60,
    Timeframe.M1: 60,
}

HARD_BOUNDARIES = frozenset(
    {
        GapClassification.MISSING_DATA,
        GapClassification.UNEXPLAINED_MISSING_DATA,
        GapClassification.PROVIDER_OUTAGE,
    }
)


@dataclass(frozen=True)
class SegmentBoundary:
    """A preserved, auditable hard gap between two clean segments."""

    reason: str
    after_utc: datetime
    before_utc: datetime
    missing_intervals: int

    def document(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "after_utc": self.after_utc,
            "before_utc": self.before_utc,
            "missing_intervals": self.missing_intervals,
        }


@dataclass(frozen=True)
class ResearchSegment:
    """One contiguous clean span derived from an audited parent dataset."""

    number: int
    dataset: CanonicalDataset
    parent_source_fingerprint: str
    parent_dataset_fingerprint: str
    boundary_before: SegmentBoundary | None
    boundary_after: SegmentBoundary | None
    eligible: bool
    excluded_reason: str | None

    @property
    def duration(self) -> timedelta:
        return self.dataset.candles[-1].timestamp_utc - self.dataset.candles[0].timestamp_utc

    def document(self) -> dict[str, object]:
        return {
            "instrument": self.dataset.instrument,
            "timeframe": self.dataset.timeframe.value,
            "source_dataset_fingerprint": self.parent_source_fingerprint,
            "parent_dataset_fingerprint": self.parent_dataset_fingerprint,
            "segment_number": self.number,
            "first_timestamp_utc": self.dataset.candles[0].timestamp_utc,
            "last_timestamp_utc": self.dataset.candles[-1].timestamp_utc,
            "candle_count": len(self.dataset.candles),
            "duration_seconds": self.duration.total_seconds(),
            "segment_fingerprint": self.dataset.dataset_fingerprint,
            "boundary_reason_before": (
                self.boundary_before.reason if self.boundary_before is not None else None
            ),
            "boundary_before": (
                self.boundary_before.document() if self.boundary_before is not None else None
            ),
            "boundary_reason_after": (
                self.boundary_after.reason if self.boundary_after is not None else None
            ),
            "boundary_after": (
                self.boundary_after.document() if self.boundary_after is not None else None
            ),
            "eligible": self.eligible,
            "excluded_reason": self.excluded_reason,
        }


@dataclass(frozen=True)
class ChronologicalSegmentPartitions:
    """Strictly ordered development, validation, and untouched-test slices."""

    development: tuple[CanonicalDataset, ...]
    validation: tuple[CanonicalDataset, ...]
    untouched_test: tuple[CanonicalDataset, ...]
    skipped_short_phase_slices: int

    @property
    def ready(self) -> bool:
        return bool(self.development and self.validation and self.untouched_test)


@dataclass(frozen=True)
class SegmentedDataset:
    """Parent audit facts and all deterministic clean research segments."""

    parent: CanonicalDataset
    segments: tuple[ResearchSegment, ...]
    hard_boundaries: tuple[SegmentBoundary, ...]

    @property
    def eligible_segments(self) -> tuple[ResearchSegment, ...]:
        return tuple(item for item in self.segments if item.eligible)

    @property
    def short_segments(self) -> tuple[ResearchSegment, ...]:
        return tuple(item for item in self.segments if not item.eligible)

    @property
    def total_raw_duration(self) -> timedelta:
        return self.parent.candles[-1].timestamp_utc - self.parent.candles[0].timestamp_utc

    @property
    def total_clean_duration(self) -> timedelta:
        return sum((item.duration for item in self.segments), timedelta())

    @property
    def usable_clean_duration(self) -> timedelta:
        return sum((item.duration for item in self.eligible_segments), timedelta())

    @property
    def clean_coverage_ratio(self) -> Decimal:
        seconds = Decimal(str(self.total_raw_duration.total_seconds()))
        if seconds <= 0:
            return Decimal("0")
        return Decimal(str(self.total_clean_duration.total_seconds())) / seconds

    @property
    def usable_clean_coverage_ratio(self) -> Decimal:
        seconds = Decimal(str(self.total_raw_duration.total_seconds()))
        if seconds <= 0:
            return Decimal("0")
        return Decimal(str(self.usable_clean_duration.total_seconds())) / seconds

    @property
    def usable_depth_sufficient(self) -> bool:
        required = timedelta(days=MINIMUM_DEPTH_DAYS[self.parent.timeframe])
        return self.usable_clean_duration >= required

    def partitions(self) -> ChronologicalSegmentPartitions:
        """Allocate clean candles by time order, never by random sampling.

        A slice that would be shorter than the reviewed phase minimum is
        excluded rather than borrowing candles across a segment boundary.
        """

        eligible = self.eligible_segments
        total = sum(len(item.dataset.candles) for item in eligible)
        development_end = int(total * Decimal("0.60"))
        validation_end = development_end + int(total * Decimal("0.20"))
        phase_ranges = (
            ("development", 0, development_end),
            ("validation", development_end, validation_end),
            ("untouched_test", validation_end, total),
        )
        groups: dict[str, list[CanonicalDataset]] = {
            "development": [],
            "validation": [],
            "untouched_test": [],
        }
        skipped = 0
        position = 0
        for segment in eligible:
            end = position + len(segment.dataset.candles)
            for phase, start_at, end_at in phase_ranges:
                slice_start = max(position, start_at)
                slice_end = min(end, end_at)
                if slice_end <= slice_start:
                    continue
                candles = segment.dataset.candles[slice_start - position : slice_end - position]
                if len(candles) < MINIMUM_PHASE_CANDLES:
                    skipped += 1
                    continue
                groups[phase].append(
                    _derived_dataset(
                        candles,
                        segment.dataset,
                        f"gap-safe-phase/{phase}/{segment.number}",
                    )
                )
            position = end
        return ChronologicalSegmentPartitions(
            development=tuple(groups["development"]),
            validation=tuple(groups["validation"]),
            untouched_test=tuple(groups["untouched_test"]),
            skipped_short_phase_slices=skipped,
        )

    def document(self) -> dict[str, object]:
        durations = sorted(item.duration.total_seconds() for item in self.segments)
        hard_missing = [item.missing_intervals for item in self.hard_boundaries]
        return {
            "instrument": self.parent.instrument,
            "timeframe": self.parent.timeframe.value,
            "source_dataset_fingerprint": self.parent.source_fingerprint,
            "parent_dataset_fingerprint": self.parent.dataset_fingerprint,
            "minimum_segment_candles": MINIMUM_SEGMENT_CANDLES,
            "minimum_phase_candles": MINIMUM_PHASE_CANDLES,
            "total_raw_duration_seconds": self.total_raw_duration.total_seconds(),
            "total_clean_duration_seconds": self.total_clean_duration.total_seconds(),
            "usable_clean_duration_seconds": self.usable_clean_duration.total_seconds(),
            "clean_coverage_ratio": self.clean_coverage_ratio,
            "usable_clean_coverage_ratio": self.usable_clean_coverage_ratio,
            "segment_count": len(self.segments),
            "eligible_segment_count": len(self.eligible_segments),
            "short_segment_count": len(self.short_segments),
            "largest_segment_duration_seconds": max(durations, default=0),
            "median_segment_duration_seconds": (durations[len(durations) // 2] if durations else 0),
            "usable_depth_sufficient": self.usable_depth_sufficient,
            "unexplained_gap_count": len(self.hard_boundaries),
            "missing_intervals": sum(hard_missing),
            "single_interval_gap_count": sum(item == 1 for item in hard_missing),
            "multi_interval_gap_count": sum(item > 1 for item in hard_missing),
            "maximum_gap_length_intervals": max(hard_missing, default=0),
            "hard_boundaries": [item.document() for item in self.hard_boundaries],
            "segments": [item.document() for item in self.segments],
            "policy": (
                "Hard missing-data boundaries split research segments. No candle, indicator "
                "state, or open trade is carried across a boundary."
            ),
        }


class GapSafeResearchSegmenter:
    """Create reviewed, deterministic clean segments from an audited dataset."""

    def segment(self, dataset: CanonicalDataset) -> SegmentedDataset:
        gap_by_before = {gap.before_utc: gap for gap in dataset.gaps}
        boundaries: list[SegmentBoundary] = []
        raw_segments: list[tuple[tuple[LabCandle, ...], SegmentBoundary | None]] = []
        start = 0
        boundary_before: SegmentBoundary | None = None
        for index, candle in enumerate(dataset.candles):
            if index == 0 or candle.gap_classification not in HARD_BOUNDARIES:
                continue
            gap = gap_by_before.get(candle.timestamp_utc)
            if gap is None:
                gap = DatasetGap(
                    after_utc=dataset.candles[index - 1].timestamp_utc,
                    before_utc=candle.timestamp_utc,
                    missing_intervals=max(
                        1,
                        int(
                            (candle.timestamp_utc - dataset.candles[index - 1].timestamp_utc)
                            / TIMEFRAME_INTERVALS[dataset.timeframe]
                        )
                        - 1,
                    ),
                    classification=candle.gap_classification,
                )
            boundary = SegmentBoundary(
                reason=candle.gap_classification.value,
                after_utc=gap.after_utc,
                before_utc=gap.before_utc,
                missing_intervals=gap.missing_intervals,
            )
            raw_segments.append((dataset.candles[start:index], boundary_before))
            boundaries.append(boundary)
            start = index
            boundary_before = boundary
        raw_segments.append((dataset.candles[start:], boundary_before))

        segments: list[ResearchSegment] = []
        for number, (candles, before) in enumerate(raw_segments, start=1):
            after = boundaries[number - 1] if number - 1 < len(boundaries) else None
            child = _derived_dataset(candles, dataset, f"gap-safe-segment/{number}")
            eligible = len(candles) >= MINIMUM_SEGMENT_CANDLES
            segments.append(
                ResearchSegment(
                    number=number,
                    dataset=child,
                    parent_source_fingerprint=dataset.source_fingerprint,
                    parent_dataset_fingerprint=dataset.dataset_fingerprint,
                    boundary_before=before,
                    boundary_after=after,
                    eligible=eligible,
                    excluded_reason=None if eligible else "SEGMENT_TOO_SHORT",
                )
            )
        return SegmentedDataset(dataset, tuple(segments), tuple(boundaries))

    @staticmethod
    def policy_document() -> dict[str, object]:
        return {
            "minimum_segment_candles": MINIMUM_SEGMENT_CANDLES,
            "minimum_phase_candles": MINIMUM_PHASE_CANDLES,
            "minimum_depth_days": {key.value: value for key, value in MINIMUM_DEPTH_DAYS.items()},
            "hard_boundary_classifications": sorted(item.value for item in HARD_BOUNDARIES),
            "end_of_segment_rule": (
                "The existing conservative END_OF_DATA trade-close behavior is applied "
                "inside each independent segment or chronological phase slice."
            ),
        }


def _derived_dataset(
    candles: tuple[LabCandle, ...], parent: CanonicalDataset, purpose: str
) -> CanonicalDataset:
    return build_dataset(
        candles,
        source_documents=(
            parent.source_fingerprint,
            parent.dataset_fingerprint,
            purpose,
            candles[0].timestamp_utc.isoformat(),
            candles[-1].timestamp_utc.isoformat(),
        ),
    )
