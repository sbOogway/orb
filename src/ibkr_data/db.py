"""SQLite market data store for candle data.

Table naming: {source}_{TICKER}_{timeframe} for candle data, e.g. ibkr_TSLA_5m.
"""

import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent.parent / "market_data.db"


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
    if timeframe == "5m":
        conn.executescript(f"""
            CREATE TRIGGER IF NOT EXISTS block_deletes_on_{tbl}
            BEFORE DELETE ON [{tbl}]
            BEGIN
                SELECT RAISE(FAIL, 'Deletion is not allowed on this table.');
            END;
        """)


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
    """Write a DataFrame with date/open/high/low/close/volume plus optional indicator columns."""
    tbl = f"{source}_{ticker.upper()}_{timeframe}"
    ensure_candle_table(db_conn, ticker, source, timeframe)

    rows = df.copy()
    rows = rows.rename(columns={"date": "ts"})
    rows["ts"] = rows["ts"].dt.strftime("%Y-%m-%dT%H:%M:%S")
    if "volume" in rows.columns:
        rows["volume"] = rows["volume"].astype(int)

    existing_cols = {row[1] for row in db_conn.execute(f"PRAGMA table_info([{tbl}])")}
    for col in rows.columns:
        if col not in existing_cols:
            db_conn.execute(f"ALTER TABLE [{tbl}] ADD COLUMN [{col}] REAL")

    col_list = ",".join(f"[{c}]" for c in rows.columns)
    placeholders = ",".join("?" for _ in rows.columns)

    db_conn.executemany(
        f"INSERT OR REPLACE INTO [{tbl}] ({col_list}) VALUES ({placeholders})",
        rows.itertuples(index=False, name=None),
    )
    db_conn.commit()
    import logging
    logging.getLogger("ibkr_data.db").info("  wrote %d rows to [%s]", len(rows), tbl)


# ---------------------------------------------------------------------------
#  Tickers table  (universe reference)
# ---------------------------------------------------------------------------

def ensure_tickers_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tickers (
            symbol     TEXT PRIMARY KEY,
            name       TEXT,
            sector     TEXT,
            market_cap REAL
        )
    """)
    # Add market_cap column if it doesn't exist (legacy tables)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tickers)")}
    if "market_cap" not in cols:
        conn.execute("ALTER TABLE tickers ADD COLUMN market_cap REAL")


def count_5m_tickers(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "select count(*) from sqlite_schema where type='table' and name like 'ibkr_%_5m'"
    ).fetchone()
    return row[0]


def get_tickers(conn: sqlite3.Connection, sector: str | None = None) -> list[str]:
    if sector:
        cur = conn.execute("SELECT symbol FROM tickers WHERE sector=? ORDER BY symbol", (sector,))
    else:
        cur = conn.execute("SELECT symbol FROM tickers ORDER BY symbol")
    return [row[0] for row in cur]
