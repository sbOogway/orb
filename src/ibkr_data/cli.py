import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from ibkr_data.db import (
    get_connection,
    ensure_candle_table,
    ensure_cache_table,
    is_day_cached,
    mark_day_cached,
    clear_ticker_cache,
    get_tickers,
    ensure_tickers_table,
    count_cached,
)

logger = logging.getLogger("ibkr_data")

_BAR_SIZES = [
    "1 min", "2 mins", "3 mins", "5 mins", "10 mins", "15 mins",
    "20 mins", "30 mins", "1 hour", "2 hours", "3 hours", "4 hours", "1 day",
]

_MAX_CHUNK_DAYS = {
    "1 min": 1, "2 mins": 2, "3 mins": 7, "5 mins": 7,
    "10 mins": 7, "15 mins": 7, "20 mins": 7, "30 mins": 30,
    "1 hour": 30, "2 hours": 30, "3 hours": 30, "4 hours": 30,
    "1 day": 365,
}


def _to_utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _chunk_days(bar_size: str) -> int:
    return _MAX_CHUNK_DAYS.get(bar_size, 7)


def _write_bars_to_db(df: pd.DataFrame, ticker: str, db_conn, source: str = "ibkr", timeframe: str = "5m"):
    """Write bars directly to the candle table, bypassing CSV files."""
    tbl = f"{source}_{ticker.upper()}_{timeframe}"
    ensure_candle_table(db_conn, ticker, source, timeframe)

    rows = df[["date", "open", "high", "low", "close", "volume"]].copy()
    rows = rows.rename(columns={"date": "ts"})
    rows["ts"] = rows["ts"].dt.strftime("%Y-%m-%dT%H:%M:%S")
    rows["volume"] = rows["volume"].astype(int)

    db_conn.executemany(
        f"INSERT OR REPLACE INTO [{tbl}] (ts,open,high,low,close,volume) VALUES (?,?,?,?,?,?)",
        rows.itertuples(index=False, name=None),
    )
    db_conn.commit()

    # mark all days in the batch as cached
    for d in rows["ts"].str[:10].unique():
        mark_day_cached(db_conn, ticker, d, timeframe, source)

    logger.info("  wrote %d rows to [%s]", len(rows), tbl)


def fetch(ticker: str, start: datetime, end: datetime, bar_size: str, host: str, port: int, client_id: int, db_conn):
    import ib_insync as ibi

    chunk_d = _chunk_days(bar_size)
    now = _to_utc(datetime.utcnow())
    target_earliest = _to_utc(start)

    ib = ibi.IB()
    ib.connect(host, port, clientId=client_id)
    logger.info("Connected to TWS at %s:%s", host, port)

    ib.reqMarketDataType(3)

    contract = ibi.Stock(ticker, "SMART", "USD")

    end_dt = _to_utc(end)
    total_chunks = 0
    chunk_num = 0
    skipped = 0

    while end_dt > target_earliest:
        chunk_start = end_dt - timedelta(days=chunk_d)
        effective_start = max(chunk_start, target_earliest)
        total_chunks += 1

        # ---- cache check: skip if every day in this range is cached ----
        cursor = effective_start
        all_cached = True
        while cursor < end_dt:
            if not is_day_cached(db_conn, ticker, cursor.strftime("%Y-%m-%d")):
                all_cached = False
                break
            cursor += timedelta(days=1)

        if all_cached:
            chunk_num += 1
            skipped += 1
            logger.info("Chunk %d/%d — %s to %s (cached, skip)", chunk_num, total_chunks, effective_start.date(), end_dt.date())
            end_dt = effective_start
            continue

        chunk_num += 1
        logger.info(
            "Chunk %d/%d — %d days ending %s ...",
            chunk_num, total_chunks, chunk_d, end_dt.strftime("%Y-%m-%d"),
        )

        bars = ib.reqHistoricalData(
            contract,
            endDateTime=end_dt.strftime("%Y%m%d %H:%M:%S"),
            durationStr=f"{chunk_d} D",
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
            keepUpToDate=False,
        )

        if not bars:
            logger.info("No more data.")
            break

        df = ibi.util.df(bars)
        df["date"] = pd.to_datetime(df["date"], unit="s", utc=True)
        df = df.sort_values("date").reset_index(drop=True)
        _write_bars_to_db(df, ticker, db_conn)

        # advance to the oldest bar in this chunk
        end_dt = _to_utc(bars[0].date)

    ib.disconnect()
    if skipped:
        logger.info("Skipped %d cached chunk(s).", skipped)
    logger.info("Fetched %d chunk(s) for %s.", chunk_num - skipped, ticker)


def setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    parser = argparse.ArgumentParser(description="Fetch historical candles from Interactive Brokers")
    parser.add_argument("--ticker", default=None, help="Ticker to fetch (default: all tickers from DB)")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD (default: 5 years ago)")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--bar-size", default="5 mins", choices=_BAR_SIZES)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7496)
    parser.add_argument("--client-id", type=int, default=1)
    parser.add_argument("--db", default=None, nargs="?", const="", help="SQLite DB path (default: market_data.db in project root)")
    parser.add_argument("--force", action="store_true", help="Drop table + clear cache before fetching")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    db_conn = get_connection(args.db if args.db else None)
    ensure_cache_table(db_conn)

    now = datetime.now(timezone.utc)
    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.end else now
    start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.start else end_dt - timedelta(days=365 * 5)
    if start_dt >= end_dt:
        logger.error("start must be before end")
        db_conn.close()
        return

    if args.ticker:
        tickers = [args.ticker.upper()]
        if args.force:
            tbl = f"ibkr_{args.ticker.upper()}_5m"
            db_conn.execute(f"DROP TABLE IF EXISTS [{tbl}]")
            clear_ticker_cache(db_conn, args.ticker)
            logger.info("--force: dropped table [%s] and cleared cache for %s", tbl, args.ticker)
    else:
        ensure_tickers_table(db_conn)
        tickers = get_tickers(db_conn)
        if not tickers:
            logger.error("No tickers found in DB. Populate the tickers table first.")
            db_conn.close()
            return
        logger.info("Loaded %d tickers from DB.", len(tickers))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # quick scan: sample ~1yr of cache days
    check_days = min(int((end_dt - start_dt).days * 5 / 7), 365)

    for ticker in tickers:
        cursor = start_dt
        fully_cached = True
        checked = 0
        while cursor < end_dt and checked < check_days:
            if not is_day_cached(db_conn, ticker, cursor.strftime("%Y-%m-%d"), timeframe="5m"):
                fully_cached = False
                break
            cursor += timedelta(days=1)
            checked += 1

        if fully_cached:
            logger.info("%s: fully cached, skipping", ticker)
            continue

        logger.info("%s: fetching ...", ticker)
        fetch(
            ticker, start_dt, end_dt, args.bar_size,
            args.host, args.port, args.client_id, db_conn,
        )

    cached = count_cached(db_conn)
    logger.info("DB cache now has %d entries.", cached)
    db_conn.close()


if __name__ == "__main__":
    main()
