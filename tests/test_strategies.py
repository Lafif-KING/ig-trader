import pandas as pd
import pytest

from src.ig_trader.models import SignalDirection
from src.ig_trader.strategies.intraday import IntradayStrategy
from src.ig_trader.strategies.scalper import ScalperStrategy


@pytest.fixture
def sample_data():
    """Create fake market data for testing."""
    data = {
        "open": [100.0] * 300,
        "high": [105.0] * 300,
        "low": [95.0] * 300,
        "close": [101.0] * 300,
        "volume": [1000] * 300,
    }
    # Create a time index
    times = pd.date_range("2024-01-01", periods=300, freq="min")
    return pd.DataFrame(data, index=times)


def test_scalper_logic(sample_data):
    """Test if Scalper returns a valid signal."""
    strategy = ScalperStrategy()
    signal = strategy.generate_signal("EURUSD", sample_data)
    assert signal.epic == "EURUSD"
    assert signal.direction in SignalDirection


def test_intraday_logic(sample_data):
    """Test if Intraday returns a valid signal."""
    strategy = IntradayStrategy()
    signal = strategy.generate_signal("EURUSD", sample_data)
    assert signal.strategy_name == "Intraday"
    assert signal.direction in SignalDirection
