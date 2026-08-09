"""Indicator helpers using 'ta' library (no numba)."""

import pandas as pd
import ta


def add_rsi(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    df["rsi"] = ta.momentum.RSIIndicator(close=df["close"], window=length).rsi()
    return df


def add_macd(
    df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    macd = ta.trend.MACD(
        close=df["close"], window_fast=fast, window_slow=slow, window_sign=signal
    )
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()
    return df


def add_atr(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    atr = ta.volatility.AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=length
    )
    df["atr"] = atr.average_true_range()
    return df


def add_bbands(df: pd.DataFrame, length: int = 20, std: int = 2) -> pd.DataFrame:
    bb = ta.volatility.BollingerBands(close=df["close"], window=length, window_dev=std)
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_middle"] = bb.bollinger_mavg()
    df["bb_upper"] = bb.bollinger_hband()
    return df
