"""Custom portfolio statistics matching sbt (github.com/sbOogway/sbt)."""

import pandas as pd
from nautilus_trader.analysis.statistic import PortfolioStatistic


class CalmarRatio(PortfolioStatistic):
    def calculate_from_returns(self, returns: pd.Series) -> float | None:
        if not self._check_valid_returns(returns):
            return None
        daily = self._downsample_to_daily_bins(returns)
        if len(daily) < 2:
            return None
        ann_return = daily.mean() * 252
        cum = (1 + daily).cumprod()
        running_max = cum.cummax()
        dd = (cum - running_max) / running_max
        max_dd = dd.min()
        if max_dd >= 0:
            return None
        return float(ann_return / abs(max_dd))


class AnnualizedReturn(PortfolioStatistic):
    def calculate_from_returns(self, returns: pd.Series) -> float | None:
        if not self._check_valid_returns(returns):
            return None
        daily = self._downsample_to_daily_bins(returns)
        if len(daily) < 2:
            return None
        return float(daily.mean() * 252)


class RunConfig(PortfolioStatistic):
    def __init__(self, **kwargs: str) -> None:
        self._kwargs = kwargs

    @property
    def name(self) -> str:
        return "Run Config"

    def calculate_from_positions(self, positions: list) -> str:
        return " | ".join(f"{k}={v}" for k, v in self._kwargs.items())
