from ta.volatility import AverageTrueRange
import pandas as pd

WINDOW = 14


def atr(df: pd.DataFrame) -> pd.DataFrame:
    return AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=WINDOW
    ).average_true_range()


def relative_volume(df: pd.DataFrame) -> pd.DataFrame:
    return (
        df["volume"] / df["volume"].shift(1).rolling(window=WINDOW, min_periods=WINDOW).mean()
    )


def average_volume(df: pd.DataFrame) -> pd.Series:
    return df["volume"].rolling(window=WINDOW, min_periods=WINDOW).mean()
