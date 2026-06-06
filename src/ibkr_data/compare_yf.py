import argparse
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

logger = logging.getLogger("ibkr_data.compare_yf")

COLUMNS = ["open", "high", "low", "close", "volume"]


def _fmt(n: float) -> str:
    if "volume" in str(n):
        return f"{n:,.0f}"
    return f"{n:,.2f}"


def _load_ibkr_5min(ticker: str, data_dir: Path, since: datetime) -> pd.DataFrame:
    frames = []
    base = data_dir / ticker
    if not base.exists():
        return pd.DataFrame()
    for year_dir in sorted(base.iterdir()):
        if not year_dir.is_dir():
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
                    df = df[df["date"] >= since]
                    if not df.empty:
                        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    return df.sort_values("date").reset_index(drop=True)


def _report(ibkr: pd.DataFrame, yf_df: pd.DataFrame, ticker: str):
    ibkr["date"] = pd.to_datetime(ibkr["date"], utc=True)
    yf_df["date"] = pd.to_datetime(yf_df["date"], utc=True)

    merged = ibkr.merge(yf_df, on="date", suffixes=("_ibkr", "_yf"), how="inner")
    if merged.empty:
        logger.warning("No overlapping 5-min bars between IBKR and yfinance for %s", ticker)
        return

    logger.info("Overlap: %d bars (%s to %s)", len(merged), merged["date"].min(), merged["date"].max())

    print()
    print(f"{'='*100}")
    print(f"  5-MIN CANDLE DIFFERENCE REPORT  —  {ticker}")
    print(f"{'='*100}")
    header = f"{'Column':<12} {'IBKR mean':>18} {'IBKR std':>18} {'YF mean':>18} {'YF std':>18} {'Mean diff':>18} {'MAE':>18} {'RMSE':>18} {'R²':>10}"
    print(header)
    print("-" * len(header))

    for col in COLUMNS:
        ib = merged[f"{col}_ibkr"]
        yf_vals = merged[f"{col}_yf"]
        diff = ib - yf_vals

        mean_ib = ib.mean()
        std_ib = ib.std()
        mean_yf = yf_vals.mean()
        std_yf = yf_vals.std()
        mean_diff = diff.mean()
        mae = diff.abs().mean()
        rmse = (diff ** 2).mean() ** 0.5
        corr = ib.corr(yf_vals)
        r2 = corr ** 2

        if col == "volume":
            print(f"{col:<12} {mean_ib:>18,.0f} {std_ib:>18,.0f} {mean_yf:>18,.0f} {std_yf:>18,.0f} {mean_diff:>+18,.0f} {mae:>18,.0f} {rmse:>18,.0f} {r2:>10.4f}")
        else:
            print(f"{col:<12} {mean_ib:>18.2f} {std_ib:>18.2f} {mean_yf:>18.2f} {std_yf:>18.2f} {mean_diff:>+18.2f} {mae:>18.2f} {rmse:>18.2f} {r2:>10.4f}")

    print(f"{'='*100}")
    close_diff = (merged["close_ibkr"] - merged["close_yf"]).abs()
    print(f"  Close price MAE:     ${close_diff.mean():.4f}")
    print(f"  Close price max |Δ|: ${close_diff.max():.4f}")
    pct_diff = ((merged["close_ibkr"] - merged["close_yf"]) / merged["close_yf"] * 100).abs()
    print(f"  Close price MAPE:    {pct_diff.mean():.4f}%")
    print(f"{'='*100}")
    print()


def fetch_and_compare(ticker: str, data_dir: Path):
    logger.info("Downloading %s 5-min data from yfinance (last 60 days) ...", ticker)
    yf_raw = yf.download(ticker, period="1mo", interval="5m", progress=False, auto_adjust=True)

    if yf_raw.empty:
        logger.warning("No yfinance data returned for %s", ticker)
        return

    yf_raw = yf_raw.reset_index()
    yf_raw.columns = [c[0] if isinstance(c, tuple) else c for c in yf_raw.columns]
    yf_raw.columns = [c.lower() for c in yf_raw.columns]
    yf_raw = yf_raw[["datetime"] + COLUMNS]
    yf_raw = yf_raw.rename(columns={"datetime": "date"})
    yf_raw["date"] = pd.to_datetime(yf_raw["date"], utc=True)
    yf_raw = yf_raw.sort_values("date").reset_index(drop=True)

    since = yf_raw["date"].min()
    logger.info("yfinance data range: %s to %s", since, yf_raw["date"].max())

    # save daily yf 5-min files
    saved = 0
    for (year, month, day), group in yf_raw.groupby([yf_raw["date"].dt.year, yf_raw["date"].dt.month, yf_raw["date"].dt.day]):
        out_dir = data_dir / ticker / str(year) / f"{month:02d}" / f"{day:02d}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "yf.csv"
        group.to_csv(out_path, index=False)
        saved += 1
    logger.info("Saved %d daily yf 5-min files", saved)

    # load matching IBKR 5-min data
    ibkr = _load_ibkr_5min(ticker, data_dir, since)
    if ibkr.empty:
        logger.warning("No IBKR 5-min data found for comparison")
        return

    logger.info("IBKR 5-min data range: %s to %s", ibkr["date"].min(), ibkr["date"].max())
    _report(ibkr, yf_raw, ticker)


def main():
    parser = argparse.ArgumentParser(description="Compare 5-min candles: IBKR vs yfinance")
    parser.add_argument("--ticker", default="TSLA")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    fetch_and_compare(args.ticker, Path(args.data_dir))


if __name__ == "__main__":
    main()
