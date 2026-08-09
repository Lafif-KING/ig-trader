"""Database management for IG Trader."""

import pandas as pd
from sqlalchemy import Column, DateTime, Float, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Create the blueprint for our tables
Base = declarative_base()

# This tells Python to create a file called 'trading.db' in your folder
DATABASE_URL = "sqlite:///./trading.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class CandleTable(Base):
    """Table to save historical prices."""

    __tablename__ = "candles"

    id = Column(Integer, primary_key=True, index=True)
    epic = Column(String)
    timestamp = Column(DateTime, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Integer)


def init_db():
    """Create the tables in the database file."""
    Base.metadata.create_all(bind=engine)


def save_candles(epic: str, df: pd.DataFrame):
    """
    Takes a pandas DataFrame of prices and saves them to the database.
    """
    session = SessionLocal()
    try:
        for timestamp, row in df.iterrows():
            # Check if this specific candle already exists to avoid duplicates
            exists = session.query(CandleTable).filter_by(epic=epic, timestamp=timestamp).first()

            if not exists:
                candle = CandleTable(
                    epic=epic,
                    timestamp=timestamp,
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row["volume"],
                )
                session.add(candle)
        session.commit()
    except Exception as e:
        session.rollback()
        print(f"Error saving to database: {e}")
    finally:
        session.close()
