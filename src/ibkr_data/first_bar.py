import argparse
import logging

import pandas as pd

from ibkr_data.db import get_connection, load_ticker_df, resolve_tickers, write_candle_table

logger = logging.getLogger("ibkr_data.first_bar")


def extract_first_bars(df: pd.DataFrame) -> pd.DataFrame:
    df["_day"] = df["date"].dt.date
    first = df.groupby("_day", sort=False).first().reset_index()
    first = first.drop(columns=["_day"])
    first.columns = ["date", "open", "high", "low", "close", "volume"]
    return first


def main():
    parser = argparse.ArgumentParser(description="Extract the first 5-min bar of each day into its own table")
    parser.add_argument("--ticker", default=None, help="Ticker (default: all tickers in DB)")
    parser.add_argument("--db", default=None, nargs="?", const="")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    db_conn = get_connection(args.db if args.db else None)
    tickers = resolve_tickers(db_conn, args.ticker)
    if not tickers:
        logger.error("No tickers found in DB.")
        return

    for ticker in tickers:
        df = load_ticker_df(db_conn, ticker, source="ibkr", timeframe="5m")
        if df.empty:
            logger.warning("%s: no 5-min data found, skipping", ticker)
            continue
        logger.info("%s: loaded %d rows (%s to %s)", ticker, len(df), df["date"].min(), df["date"].max())
        first_bars = extract_first_bars(df)
        write_candle_table(first_bars, ticker, db_conn, timeframe="5m_first")

    db_conn.close()


if __name__ == "__main__":
    main()
