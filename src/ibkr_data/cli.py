import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pandas_market_calendars as mcal

from ibkr_data.db import get_connection, ensure_candle_table, table_name, get_tickers, ensure_tickers_table

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

_SOURCE = "ibkr"
_TIMEFRAME = "5m"


def _to_utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _chunk_days(bar_size: str) -> int:
    return _MAX_CHUNK_DAYS.get(bar_size, 7)


def _date_range_covered(ticker: str, db_conn, start: datetime, end: datetime) -> bool:
    """Return True if every trading day between start and end has some bars."""
    tbl = table_name(ticker, _SOURCE, _TIMEFRAME)
    try:
        cur = db_conn.execute(
            f"SELECT COUNT(DISTINCT DATE(ts)) FROM [{tbl}] WHERE ts >= ? AND ts < ?",
            (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
        )
        existing = cur.fetchone()[0]
    except Exception:
        return False

    cal = mcal.get_calendar("NYSE")
    schedule = cal.schedule(start_date=start.date(), end_date=(end - timedelta(days=1)).date())
    expected = len(schedule)
    return existing >= expected if expected > 0 else False


def _write_bars_to_db(df: pd.DataFrame, ticker: str, db_conn):
    tbl = table_name(ticker, _SOURCE, _TIMEFRAME)
    ensure_candle_table(db_conn, ticker, _SOURCE, _TIMEFRAME)

    rows = df[["date", "open", "high", "low", "close", "volume"]].copy()
    rows = rows.rename(columns={"date": "ts"})
    rows["ts"] = rows["ts"].dt.strftime("%Y-%m-%dT%H:%M:%S")
    rows["volume"] = rows["volume"].astype(int)

    db_conn.executemany(
        f"INSERT OR REPLACE INTO [{tbl}] (ts,open,high,low,close,volume) VALUES (?,?,?,?,?,?)",
        rows.itertuples(index=False, name=None),
    )
    db_conn.commit()

    logger.info("  wrote %d rows to [%s]", len(rows), tbl)


def fetch(ticker: str, start: datetime, end: datetime, bar_size: str, host: str, port: int, client_id: int, db_conn):
    import ib_insync as ibi

    chunk_d = _chunk_days(bar_size)
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

        if _date_range_covered(ticker, db_conn, effective_start, end_dt):
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
    parser.add_argument("--force", action="store_true", help="Drop table before fetching")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    db_conn = get_connection(args.db if args.db else None)

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
            tbl = table_name(args.ticker.upper(), _SOURCE, _TIMEFRAME)
            db_conn.execute(f"DROP TABLE IF EXISTS [{tbl}]")
            logger.info("--force: dropped table [%s]", tbl)
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

    for ticker in tickers:
        if _date_range_covered(ticker, db_conn, start_dt, end_dt):
            logger.info("%s: fully cached, skipping", ticker)
            continue

        logger.info("%s: fetching ...", ticker)
        fetch(
            ticker, start_dt, end_dt, args.bar_size,
            args.host, args.port, args.client_id, db_conn,
        )

    db_conn.close()
