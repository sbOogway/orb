"""SQLite market data store for candle data.

Table naming: {source}_{TICKER}_{timeframe} for candle data, e.g. ibkr_TSLA_5m.
Plus cache and tickers metadata tables.
"""

import sqlite3
from pathlib import Path
from typing import Optional

import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent.parent / "market_data.db"
SOURCE_NAMES = frozenset({"ibkr", "yf", "polygon"})


# ---------------------------------------------------------------------------
#  Connection
# ---------------------------------------------------------------------------

def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# ---------------------------------------------------------------------------
#  Candle tables  ({source}_{TICKER}_{timeframe})
# ---------------------------------------------------------------------------

def table_name(ticker: str, source: str = "ibkr", timeframe: str = "5m") -> str:
    return f"{source}_{ticker.upper()}_{timeframe}"


def ensure_candle_table(conn: sqlite3.Connection, ticker: str, source: str = "ibkr", timeframe: str = "5m"):
    tbl = table_name(ticker, source, timeframe)
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS [{tbl}] (
            ts TEXT NOT NULL PRIMARY KEY,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume INTEGER NOT NULL
        );
    """)


def import_csv(
    csv_path: str | Path,
    conn: sqlite3.Connection,
    ticker: str,
    source: str = "ibkr",
    timeframe: str = "5m",
) -> int:
    """Import a single CSV into its candle table. Returns rows inserted."""
    tbl = table_name(ticker, source, timeframe)
    ensure_candle_table(conn, ticker, source, timeframe)

    df = pd.read_csv(csv_path)
    df.columns = [c.strip().lower() for c in df.columns]
    if "date" in df.columns:
        df = df.rename(columns={"date": "ts"})

    cols = ["ts", "open", "high", "low", "close", "volume"]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        return 0

    sub = df[cols].copy()
    sub["volume"] = sub["volume"].astype(int)

    conn.executemany(
        f"INSERT OR REPLACE INTO [{tbl}] (ts,open,high,low,close,volume) VALUES (?,?,?,?,?,?)",
        sub.itertuples(index=False, name=None),
    )
    conn.commit()
    return len(sub)


def scan_and_import(data_dir: str | Path, db_path: str | Path | None = None) -> dict[str, int]:
    """Scan CSV folder, import everything into candle tables. Returns {table: row_count}."""
    totals: dict[str, int] = {}
    root = Path(data_dir)
    conn = get_connection(db_path)

    for ticker_dir in sorted(root.iterdir()):
        if not ticker_dir.is_dir():
            continue
        ticker = ticker_dir.name.upper()
        for year_dir in sorted(ticker_dir.iterdir()):
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            for month_dir in sorted(year_dir.iterdir()):
                if not month_dir.is_dir():
                    continue
                for day_dir in sorted(month_dir.iterdir()):
                    for csv_path in day_dir.glob("*.csv"):
                        source = csv_path.stem
                        if source not in SOURCE_NAMES:
                            continue
                        rows = import_csv(csv_path, conn, ticker, source)
                        tbl = table_name(ticker, source)
                        totals[tbl] = totals.get(tbl, 0) + rows

    conn.close()
    return totals


def load_ticker_df(
    conn: sqlite3.Connection,
    ticker: str,
    source: str = "ibkr",
    timeframe: str = "5m",
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Load candles as DataFrame with columns date,open,high,low,close,volume."""
    tbl = table_name(ticker, source, timeframe)
    query = f"SELECT ts, open, high, low, close, volume FROM [{tbl}]"
    params: list[str] = []
    conds: list[str] = []
    if start:
        conds.append("ts >= ?")
        params.append(start)
    if end:
        conds.append("ts <= ?")
        params.append(f"{end}T23:59:59")
    if conds:
        query += " WHERE " + " AND ".join(conds)
    query += " ORDER BY ts"

    df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        return df
    df = df.rename(columns={"ts": "date"})
    df["date"] = pd.to_datetime(df["date"], utc=True, format="ISO8601")
    return df


def resolve_tickers(db_conn, ticker_arg: str | None = None) -> list[str]:
    """Return tickers from the --ticker argument or the tickers table."""
    if ticker_arg:
        return [ticker_arg.upper()]
    ensure_tickers_table(db_conn)
    tickers = get_tickers(db_conn)
    return tickers


def write_candle_table(df: pd.DataFrame, ticker: str, db_conn, source: str = "ibkr", timeframe: str = "1d"):
    """Write a DataFrame with columns date,open,high,low,close,volume to a candle table."""
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
    import logging
    logging.getLogger("ibkr_data.db").info("  wrote %d rows to [%s]", len(rows), tbl)


# ---------------------------------------------------------------------------
#  Cache table  (tracks what days have been scraped)
# ---------------------------------------------------------------------------

def ensure_cache_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            ticker    TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            day       TEXT NOT NULL,
            source    TEXT NOT NULL DEFAULT 'ibkr',
            PRIMARY KEY (ticker, timeframe, day, source)
        )
    """)


def is_day_cached(conn: sqlite3.Connection, ticker: str, day: str, timeframe: str = "5m", source: str = "ibkr") -> bool:
    cur = conn.execute(
        "SELECT 1 FROM cache WHERE ticker=? AND timeframe=? AND day=? AND source=?",
        (ticker.upper(), timeframe, day, source),
    )
    return cur.fetchone() is not None


def mark_day_cached(conn: sqlite3.Connection, ticker: str, day: str, timeframe: str = "5m", source: str = "ibkr"):
    conn.execute(
        "INSERT OR IGNORE INTO cache (ticker, timeframe, day, source) VALUES (?, ?, ?, ?)",
        (ticker.upper(), timeframe, day, source),
    )
    conn.commit()


def clear_day_cache(conn: sqlite3.Connection, ticker: str, day: str, timeframe: str = "5m", source: str = "ibkr"):
    conn.execute(
        "DELETE FROM cache WHERE ticker=? AND timeframe=? AND day=? AND source=?",
        (ticker.upper(), timeframe, day, source),
    )
    conn.commit()


def clear_ticker_cache(conn: sqlite3.Connection, ticker: str):
    """Delete ALL cache entries for a ticker."""
    conn.execute("DELETE FROM cache WHERE ticker=?", (ticker.upper(),))
    conn.commit()


def count_cached(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]


# ---------------------------------------------------------------------------
#  Tickers table  (universe reference)
# ---------------------------------------------------------------------------

def ensure_tickers_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tickers (
            symbol TEXT PRIMARY KEY,
            name   TEXT,
            sector TEXT
        )
    """)


def upsert_tickers(conn: sqlite3.Connection, tickers: list[tuple[str, str, str]]):
    """Insert or update tickers. Each tuple is (symbol, name, sector)."""
    conn.executemany(
        "INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES (?, ?, ?)",
        tickers,
    )
    conn.commit()


def get_tickers(conn: sqlite3.Connection, sector: str | None = None) -> list[str]:
    if sector:
        cur = conn.execute("SELECT symbol FROM tickers WHERE sector=? ORDER BY symbol", (sector,))
    else:
        cur = conn.execute("SELECT symbol FROM tickers ORDER BY symbol")
    return [row[0] for row in cur]
