import argparse
import asyncio
import logging
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from db import get_connection, ensure_cache_table, is_day_cached, mark_day_cached, clear_ticker_cache, count_cached

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


def _save_daily_chunk(df: pd.DataFrame, ticker: str, data_dir: Path, db_conn):
    for (year, month, day), group in df.groupby([df["date"].dt.year, df["date"].dt.month, df["date"].dt.day]):
        day_str = f"{year}-{month:02d}-{day:02d}"
        out_dir = data_dir / ticker / str(year) / f"{month:02d}" / f"{day:02d}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "ibkr.csv"
        exists = out_path.exists()
        group.to_csv(out_path, index=False)
        mark_day_cached(db_conn, ticker, day_str)
        logger.info("  %s %s (%d rows)", "updated" if exists else "saved", out_path, len(group))


def fetch(ticker: str, years: int, bar_size: str, host: str, port: int, client_id: int, data_dir: Path, db_conn):
    import ib_insync as ibi

    chunk_d = _chunk_days(bar_size)
    now = _to_utc(datetime.utcnow())
    target_earliest = now - timedelta(days=365 * years)

    ib = ibi.IB()
    ib.connect(host, port, clientId=client_id)
    logger.info("Connected to TWS at %s:%s", host, port)

    ib.reqMarketDataType(3)

    contract = ibi.Stock(ticker, "SMART", "USD")

    end_dt = now
    total_chunks = (365 * years + chunk_d - 1) // chunk_d
    chunk_num = 0
    skipped = 0

    while end_dt > target_earliest:
        chunk_start = end_dt - timedelta(days=chunk_d)
        effective_start = max(chunk_start, target_earliest)

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
        _save_daily_chunk(df, ticker, data_dir, db_conn)

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
    parser.add_argument("--ticker", default="TSLA")
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--bar-size", default="5 mins", choices=_BAR_SIZES)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7496)
    parser.add_argument("--client-id", type=int, default=1)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--db", default=None, nargs="?", const="", help="SQLite DB path (default: market_data.db in project root)")
    parser.add_argument("--force", action="store_true", help="Ignore cache, re-fetch all data")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    db_conn = get_connection(args.db if args.db else None)
    ensure_cache_table(db_conn)

    if args.force:
        import shutil
        ticker_dir = Path(args.data_dir) / args.ticker
        if ticker_dir.is_dir():
            logger.info("--force: removing cached data for %s", args.ticker)
            shutil.rmtree(ticker_dir)
        clear_ticker_cache(db_conn, args.ticker)
        logger.info("--force: cleared cache entries for %s", args.ticker)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    fetch(
        args.ticker, args.years, args.bar_size,
        args.host, args.port, args.client_id, Path(args.data_dir), db_conn,
    )

    cached = count_cached(db_conn)
    logger.info("DB cache now has %d entries.", cached)
    db_conn.close()


if __name__ == "__main__":
    main()
