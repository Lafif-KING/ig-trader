"""Conservative, reproducible SL-03 market-closure classification."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

from src.ig_trader.strategy_lab.data import (
    CanonicalDataset,
    DatasetGap,
    GapClassification,
    LabCandle,
    build_dataset,
)
from src.ig_trader.strategy_lab.models import AssetClass

EXPECTED_WEEKEND = "EXPECTED_WEEKEND"
EXPECTED_SESSION = "EXPECTED_MARKET_SESSION_CLOSE"
EXPECTED_HOLIDAY = "EXPECTED_HOLIDAY_OR_EXCHANGE_CLOSE"
PROVIDER_OUTAGE = "PROVIDER_OUTAGE"
UNEXPLAINED = "UNEXPLAINED_MISSING_DATA"
SOURCE_TRUNCATION = "SOURCE_TRUNCATION"
INVALID_ROW = "INVALID_ROW"
DUPLICATE = "DUPLICATE"
OUT_OF_ORDER = "OUT_OF_ORDER"


@dataclass(frozen=True)
class AuditedDataset:
    """A derived dataset plus parent provenance and individual gap decisions."""

    dataset: CanonicalDataset
    parent_source_fingerprint: str
    parent_dataset_fingerprint: str
    gaps: tuple[dict[str, object], ...]

    @property
    def unexplained_gap_count(self) -> int:
        return sum(item["classification"] == UNEXPLAINED for item in self.gaps)


def audit_dataset(dataset: CanonicalDataset, asset_class: AssetClass) -> AuditedDataset:
    """Classify only objectively expected closures; every other gap stays blocking.

    The recurring-session evidence rule is deliberately narrow: it requires the
    exact UTC transition (weekday, hour, minute, and missing interval count) to
    occur at least three times in this dataset.  It is not a profitability- or
    candidate-dependent relaxation.
    """

    counts = Counter(_transition_signature(gap) for gap in dataset.gaps)
    classification_by_after: dict[object, str] = {}
    rows: list[dict[str, object]] = []
    for gap in dataset.gaps:
        classification = _classify_gap(gap, asset_class, counts)
        classification_by_after[gap.before_utc] = classification
        rows.append(
            {
                "after_utc": gap.after_utc,
                "before_utc": gap.before_utc,
                "missing_intervals": gap.missing_intervals,
                "classification": classification,
                "transition_occurrences": counts[_transition_signature(gap)],
            }
        )
    candles = tuple(
        _with_gap_classification(candle, classification_by_after.get(candle.timestamp_utc))
        for candle in dataset.candles
    )
    derived = build_dataset(
        candles,
        source_documents=(
            dataset.source_fingerprint,
            dataset.dataset_fingerprint,
            "sl03-gap-audit-policy/1.0",
            asset_class.value,
        ),
    )
    return AuditedDataset(
        dataset=derived,
        parent_source_fingerprint=dataset.source_fingerprint,
        parent_dataset_fingerprint=dataset.dataset_fingerprint,
        gaps=tuple(rows),
    )


def _with_gap_classification(candle: LabCandle, classification: str | None) -> LabCandle:
    if classification is None:
        return candle
    gap_classification = (
        GapClassification.WEEKEND_OR_SESSION
        if classification in {EXPECTED_WEEKEND, EXPECTED_SESSION, EXPECTED_HOLIDAY}
        else GapClassification.UNEXPLAINED_MISSING_DATA
    )
    return LabCandle(
        instrument=candle.instrument,
        timestamp_utc=candle.timestamp_utc,
        timeframe=candle.timeframe,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        spread=candle.spread,
        volume=candle.volume,
        source=candle.source,
        source_quality=candle.source_quality,
        gap_classification=gap_classification,
        synthetic=candle.synthetic,
    )


def _classify_gap(
    gap: DatasetGap,
    asset_class: AssetClass,
    counts: Counter[tuple[int, int, int, int, int, int, int]],
) -> str:
    if _crosses_weekend(gap):
        return EXPECTED_WEEKEND
    if asset_class is AssetClass.INDEX and _crosses_us_exchange_holiday(gap):
        return EXPECTED_HOLIDAY
    if (
        asset_class in {AssetClass.METAL, AssetClass.INDEX}
        and counts[_transition_signature(gap)] >= 3
    ):
        return EXPECTED_SESSION
    return UNEXPLAINED


def _transition_signature(gap: DatasetGap) -> tuple[int, int, int, int, int, int, int]:
    return (
        gap.after_utc.weekday(),
        gap.after_utc.hour,
        gap.after_utc.minute,
        gap.before_utc.weekday(),
        gap.before_utc.hour,
        gap.before_utc.minute,
        gap.missing_intervals,
    )


def _crosses_weekend(gap: DatasetGap) -> bool:
    """Recognise only a Friday-to-Sunday/Monday span, never a weekday outage."""

    return gap.after_utc.weekday() == 4 and gap.before_utc.weekday() in {0, 6}


def _crosses_us_exchange_holiday(gap: DatasetGap) -> bool:
    first = gap.after_utc.date()
    last = gap.before_utc.date()
    current = first
    while current <= last:
        if _is_us_exchange_holiday(current):
            return True
        current += timedelta(days=1)
    return False


def _is_us_exchange_holiday(value: date) -> bool:
    """US regular-session closure dates used solely for index gap auditing."""

    year = value.year
    fixed = {
        _observed(date(year, 1, 1)),
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _observed(date(year, 12, 25)),
    }
    movable = {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Presidents Day
        _last_weekday(year, 5, 0),  # Memorial Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
    }
    return value in fixed | movable


def _observed(value: date) -> date:
    if value.weekday() == 5:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    result = date(year, month, 1)
    result += timedelta(days=(weekday - result.weekday()) % 7 + (occurrence - 1) * 7)
    return result


def _last_weekday(year: int, month: int, weekday: int) -> date:
    result = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return result - timedelta(days=(result.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Gregorian computus; deterministic and dependency-free."""

    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    day = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * day) // 451
    month, day_of_month = divmod(h + day - 7 * m + 114, 31)
    return date(year, month, day_of_month + 1)
