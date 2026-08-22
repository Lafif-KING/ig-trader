from datetime import UTC, datetime, timedelta

import pytest

from src.ig_trader.shadow_candles import FinalMinuteCandleBuilder, QuoteTick


NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
EPIC = "CS.D.EURGBP.MINI.IP"


def tick(seconds: int, bid: float = 0.85, offer: float = 0.8501) -> QuoteTick:
    return QuoteTick(EPIC, bid, offer, NOW + timedelta(seconds=seconds))


def test_builder_emits_only_final_contiguous_minutes_and_bounds_memory() -> None:
    builder = FinalMinuteCandleBuilder(EPIC, capacity=60)
    assert builder.add(tick(0)) is None
    assert builder.add(tick(30, 0.8501, 0.8502)) is None
    candle = builder.add(tick(60, 0.8502, 0.8503))
    assert candle is not None
    assert candle.bid_open == 0.85
    assert candle.bid_close == 0.8501
    for minute in range(2, 65):
        builder.add(tick(minute * 60))
    assert len(builder.finalized) == 60


def test_gap_and_partial_data_do_not_create_synthetic_candles() -> None:
    builder = FinalMinuteCandleBuilder(EPIC)
    builder.add(tick(0))
    assert builder.add(tick(120)) is None
    assert builder.finalized == ()


def test_invalid_or_out_of_order_ticks_are_rejected() -> None:
    builder = FinalMinuteCandleBuilder(EPIC)
    with pytest.raises(ValueError):
        builder.add(QuoteTick(EPIC, 0.8501, 0.85, NOW))
    builder.add(tick(60))
    with pytest.raises(ValueError):
        builder.add(tick(0))
