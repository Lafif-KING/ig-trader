import pandas as pd

from src.ig_trader.database import CandleTable, SessionLocal, save_candles


def test_save_and_retrieve_candles():
    """Verify that we can save a candle and read it back from the database."""
    # 1. Create fake data
    data = {
        "open": [1.10],
        "high": [1.12],
        "low": [1.08],
        "close": [1.11],
        "volume": [100],
    }
    times = pd.date_range("2024-01-01", periods=1, freq="min", tz="UTC")
    df = pd.DataFrame(data, index=times)

    # 2. Save it
    save_candles("TEST_EPIC", df)

    # 3. Try to find it in the DB
    session = SessionLocal()
    saved_candle = session.query(CandleTable).filter_by(epic="TEST_EPIC").first()
    session.close()

    assert saved_candle is not None
    assert saved_candle.close == 1.11
