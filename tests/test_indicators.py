import pandas as pd

from src.ig_trader.indicators import add_atr, add_bbands, add_macd, add_rsi


def test_indicators_add_columns() -> None:
    data = {
        "time": pd.date_range("2024-01-01", periods=50, freq="min", tz="UTC"),
        "open": [1 + i * 0.001 for i in range(50)],
        "high": [1.01 + i * 0.001 for i in range(50)],
        "low": [0.99 + i * 0.001 for i in range(50)],
        "close": [1 + i * 0.001 for i in range(50)],
        "volume": [100 + i for i in range(50)],
    }
    df = pd.DataFrame(data).set_index("time")
    df = add_rsi(df)
    df = add_macd(df)
    df = add_atr(df)
    df = add_bbands(df)
    expected_cols = [
        "rsi",
        "macd",
        "macd_signal",
        "macd_hist",
        "atr",
        "bb_lower",
        "bb_middle",
        "bb_upper",
    ]
    for col in expected_cols:
        assert col in df.columns
