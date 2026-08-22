import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.ig_trader import database as database_module


def test_save_and_retrieve_candles(tmp_path, monkeypatch):
    """Verify that we can save a candle and read it back from the database."""
    test_engine = create_engine(
        f"sqlite:///{(tmp_path / 'candles-test.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    test_session_local = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    monkeypatch.setattr(database_module, "engine", test_engine)
    monkeypatch.setattr(database_module, "SessionLocal", test_session_local)
    database_module.Base.metadata.create_all(bind=test_engine)

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
    database_module.save_candles("TEST_EPIC", df)

    # 3. Try to find it in the DB
    try:
        session = test_session_local()
        try:
            saved_candle = (
                session.query(database_module.CandleTable).filter_by(epic="TEST_EPIC").first()
            )
        finally:
            session.close()
    finally:
        test_engine.dispose()

    assert saved_candle is not None
    assert saved_candle.close == 1.11
