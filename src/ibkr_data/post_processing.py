import argparse
import logging
import re

import pandas as pd

from ibkr_data.db import get_connection, load_ticker_df, resolve_tickers, write_candle_table
from ibkr_data.indicators import atr, average_volume, relative_volume

logger = logging.getLogger("ibkr_data.post_processing")


def extract_first_bars(df: pd.DataFrame) -> pd.DataFrame:
    df["_day"] = df["date"].dt.date
    first = df.groupby("_day", sort=False).first().reset_index()
    first = first.drop(columns=["_day"])
    first.columns = ["date", "open", "high", "low", "close", "volume"]
    first["date"] = pd.to_datetime(first["date"])
    return first


def resample_to_daily(df: pd.DataFrame) -> pd.DataFrame:
    daily = df.resample("1D", on="date").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).dropna().reset_index()
    daily["date"] = pd.to_datetime(daily["date"].dt.date)
    return daily


def main():
    parser = argparse.ArgumentParser(description="Post-processing: resample to daily and/or extract first bar of each day")
    parser.add_argument("--ticker", default=None, help="Ticker (default: all tickers in DB)")
    parser.add_argument("--db", default=None, nargs="?", const="")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--first-bar", action="store_true", help="Only extract first 5-min bar")
    parser.add_argument("--resample-daily", action="store_true", help="Only resample to daily")
    parser.add_argument("--drop", action="store_true", help="Drop all derived tables (*_5m_first, *_1d)")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    db_conn = get_connection(args.db if args.db else None)

    if args.drop:
        tables = db_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        to_drop = [tbl for (tbl,) in tables if re.search(r"_(5m_first|1d)$", tbl)]
        if not to_drop:
            logger.info("No derived tables found.")
        else:
            logger.info("Dropping %d table(s):", len(to_drop))
            for tbl in to_drop:
                logger.info("  DROP TABLE [%s]", tbl)
                db_conn.execute(f"DROP TABLE IF EXISTS [{tbl}]")
            db_conn.commit()
            logger.info("Done.")
        db_conn.close()
        return

    do_first_bar = args.first_bar or not args.resample_daily
    do_resample = args.resample_daily or not args.first_bar

    tickers = resolve_tickers(db_conn, args.ticker)
    if not tickers:
        logger.error("No tickers found in DB.")
        db_conn.close()
        return

    for ticker in tickers:
        try:
            df = load_ticker_df(db_conn, ticker, source="ibkr", timeframe="5m")
        except Exception:
            logger.warning("%s: no 5-min table found, skipping", ticker)
            continue
        if df.empty:
            logger.warning("%s: no 5-min data found, skipping", ticker)
            continue
        logger.info("%s: loaded %d rows (%s to %s)", ticker, len(df), df["date"].min(), df["date"].max())

        if do_resample:
            daily = resample_to_daily(df)
            daily["atr"] = atr(daily)
            daily["average_volume"] = average_volume(daily)
            write_candle_table(daily, ticker, db_conn, timeframe="1d")

        if do_first_bar:
            first_bars = extract_first_bars(df)
            first_bars["relative_volume"] = relative_volume(first_bars)
            write_candle_table(first_bars, ticker, db_conn, timeframe="5m_first")

    db_conn.close()


if __name__ == "__main__":
    main()
