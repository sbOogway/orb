import argparse
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger("ibkr_data.resample")


def _walk_5min_csvs(ticker: str, data_dir: Path) -> pd.DataFrame:
    frames = []
    base = data_dir / ticker
    if not base.exists():
        logger.error("Directory not found: %s", base)
        return pd.DataFrame()
    for year_dir in sorted(base.iterdir()):
        if not year_dir.is_dir() or year_dir.name == "daily":
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir():
                continue
            for day_dir in sorted(month_dir.iterdir()):
                if not day_dir.is_dir():
                    continue
                csv_file = day_dir / "ibkr.csv"
                if csv_file.exists():
                    df = pd.read_csv(csv_file, parse_dates=["date"])
                    frames.append(df)
    if not frames:
        logger.warning("No 5-min CSV files found under %s/.../<day>/ibkr.csv", base)
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("date").reset_index(drop=True)
    return df


def resample_to_daily(df: pd.DataFrame) -> pd.DataFrame:
    daily = df.resample("1D", on="date").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        barCount=("barCount", "sum"),
    ).dropna().reset_index()
    daily["date"] = pd.to_datetime(daily["date"].dt.date)
    return daily


def _save_monthly(daily: pd.DataFrame, ticker: str, data_dir: Path):
    for (year, month), group in daily.groupby([daily["date"].dt.year, daily["date"].dt.month]):
        out_dir = data_dir / ticker / str(year) / f"{month:02d}" / "day"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "ibkr.csv"
        group.to_csv(out_path, index=False)
        logger.info("  %s (%d rows)", out_path, len(group))


def main():
    parser = argparse.ArgumentParser(description="Resample 5-min candles to daily IBKR data")
    parser.add_argument("--ticker", default="TSLA")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    df = _walk_5min_csvs(args.ticker, Path(args.data_dir))
    if df.empty:
        return

    logger.info("Loaded %d rows of 5-min data (%s to %s)", len(df), df["date"].min(), df["date"].max())

    daily = resample_to_daily(df)
    logger.info("Resampled to %d daily rows", len(daily))

    _save_monthly(daily, args.ticker, Path(args.data_dir))


if __name__ == "__main__":
    main()
