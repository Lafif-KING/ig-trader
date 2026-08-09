"""Database management for IG Trader."""

import pandas as pd
from sqlalchemy import Column, DateTime, Float, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# SQLAlchemy base and engine
Base = declarative_base()
DATABASE_URL = "sqlite:///./trading.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class CandleTable(Base):
    """Table to save historical prices."""

    __tablename__ = "candles"

    id = Column(Integer, primary_key=True, index=True)
    epic = Column(String, index=True)
    timestamp = Column(DateTime, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Integer)


def init_db() -> None:
    """Create the tables in the database file."""
    Base.metadata.create_all(bind=engine)


def save_candles(epic: str, df: pd.DataFrame) -> None:
    """Persist a DataFrame of candles into the database (idempotent per epic+timestamp)."""
    session = SessionLocal()
    try:
        for ts, row in df.iterrows():
            # Normalize timestamp (pandas Timestamp -> datetime)
            ts_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts

            exists = (
                session.query(CandleTable).filter_by(epic=epic, timestamp=ts_dt).first()
            )
            if exists:
                continue

            candle = CandleTable(
                epic=epic,
                timestamp=ts_dt,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]) if pd.notna(row["volume"]) else None,
            )
            session.add(candle)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"Error saving to database: {e}")
    finally:
        session.close()
