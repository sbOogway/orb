"""Reusable backtest logic — fees, signals, engine, reports.

Designed for both multi-stock (portfolio) and single-stock modes.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pandas_market_calendars as mcal
import quantstats as qs

# Suppress matplotlib font warnings (Arial not found on Linux)
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
from sqlite3 import OperationalError

from ibkr_data.db import (
    table_name,
    get_connection,
    count_5m_tickers,
)

# ═══════════════════════════════════════════════════════════════
# Fees
# ═══════════════════════════════════════════════════════════════

# IBKR Pro Tiered (US stocks, ≤ 300K shares/month)
COMMISSION_PER_SHARE = 0.0035
EXCHANGE_FEE = 0.0030
CLEARING_FEE = 0.0002
FINRA_CAT = 0.000003
SEC_FEE_RATE = 0.0000206
MINIMUM_PER_ORDER = 0.35
MAXIMUM_PER_ORDER_PCT = 0.01

MONTHLY_DATA_FEE = 4.5

FEE_PER_SHARE_TOTAL = round(
    COMMISSION_PER_SHARE + EXCHANGE_FEE + CLEARING_FEE + FINRA_CAT, 4
)


def trade_cost(shares: int, price: float, sell: bool = False) -> float:
    """IBKR Pro Tiered all-in cost for one side of a US stock trade."""
    per_share = COMMISSION_PER_SHARE + EXCHANGE_FEE + CLEARING_FEE + FINRA_CAT
    fee = max(shares * per_share, MINIMUM_PER_ORDER)
    fee = min(fee, shares * price * MAXIMUM_PER_ORDER_PCT)
    if sell:
        fee += shares * price * SEC_FEE_RATE
    return fee


# ═══════════════════════════════════════════════════════════════
# Market calendar
# ═══════════════════════════════════════════════════════════════

def get_market_days(
    start: datetime.date | None = None,
    end: datetime.date | None = None,
    calendar: str = "NYSE",
) -> list[datetime.date]:
    """Return sorted list of NYSE market days between start and end."""
    if start is None:
        start = (datetime.now(timezone.utc) - timedelta(days=365 * 5)).date()
    if end is None:
        end = datetime.now(timezone.utc).date()
    cal = mcal.get_calendar(calendar)
    schedule = cal.schedule(start_date=start, end_date=end)
    return [d.date() for d in schedule.index]


# ═══════════════════════════════════════════════════════════════
# Ticker fetching
# ═══════════════════════════════════════════════════════════════

TickerQueryParams = dict[str, str | int]


def fetch_tickers(
    conn,
    order_by: str = "random()",
    limit: int = 100,
) -> tuple[list[str], TickerQueryParams]:
    """Fetch tickers that have 5m candle data, ordered and limited.

    Returns (ticker_list, params_dict) where params_dict captures the
    selection criteria for display / logging.
    """
    params: TickerQueryParams = {"order_by": order_by, "limit": limit}
    rows = conn.execute(
        f"""
        SELECT symbol FROM tickers t
        WHERE 'ibkr_' || t.symbol || '_5m' IN (
            SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'ibkr_%_5m'
        )
        ORDER BY {order_by}
        LIMIT ?;
        """,
        (limit,),
    ).fetchall()
    return [r[0] for r in rows], params


# ═══════════════════════════════════════════════════════════════
# Data loading helpers
# ═══════════════════════════════════════════════════════════════

def load_first_bar(conn, ticker: str, day: datetime.date) -> tuple | None:
    """Return (ts, open, high, low, close, relative_volume) or None."""
    tbl = table_name(ticker, timeframe="5m_first")
    try:
        return conn.execute(
            f"SELECT ts, open, high, low, close, relative_volume FROM [{tbl}] "
            "WHERE date(ts) = ? AND relative_volume IS NOT NULL",
            (day.isoformat(),),
        ).fetchone()
    except OperationalError:
        return None


def load_daily_row(conn, ticker: str, day: datetime.date) -> tuple | None:
    """Return (atr, close) from the 1d table or None."""
    tbl = table_name(ticker, timeframe="1d")
    try:
        return conn.execute(
            f"SELECT atr, close FROM [{tbl}] WHERE date(ts) = ?",
            (day.isoformat(),),
        ).fetchone()
    except OperationalError:
        return None


def load_intraday_bars(conn, ticker: str, day: datetime.date) -> list[tuple]:
    """Return list of (ts, open, high, low, close) sorted ascending."""
    tbl = table_name(ticker, timeframe="5m")
    try:
        return conn.execute(
            f"SELECT ts, open, high, low, close FROM [{tbl}] "
            "WHERE date(ts) = ? ORDER BY ts",
            (day.isoformat(),),
        ).fetchall()
    except OperationalError:
        return []


# ═══════════════════════════════════════════════════════════════
# Signal & sizing
# ═══════════════════════════════════════════════════════════════

SignalDirection = int  # 1 = long, -1 = short, 0 = none


def compute_signal(first_bar: tuple) -> SignalDirection:
    """Determine long/short direction from the first 5m bar.

    Returns 1 (long), -1 (short), or 0 (no signal).
    """
    first_open = first_bar[1]
    first_close = first_bar[4]
    delta = np.sign(first_close - first_open)
    if delta > 0:
        return 1  # long
    if delta < 0:
        return -1  # short
    return 0


def compute_entry_stop(
    first_bar: tuple,
    atr_value: float,
    atr_distance: float,
    direction: SignalDirection,
) -> tuple[float, float] | None:
    """Return (entry_price, stop_loss_price) or None if ATR is invalid."""
    if atr_value is None or np.isnan(atr_value):
        return None
    stop_loss_distance = atr_value * atr_distance
    first_low = first_bar[3]
    first_high = first_bar[2]
    if direction == -1:  # short
        return first_low, first_low + stop_loss_distance
    else:  # long
        return first_high, first_high - stop_loss_distance


def compute_shares(
    portfolio_value: float,
    risk_per_trade: float,
    entry_price: float,
    stop_price: float,
    cost_fn: Callable[[int, float, bool], float] | None = None,
    sell: bool = False,
) -> int:
    """Position size in shares based on fixed-fractional risk."""
    risk_distance = abs(stop_price - entry_price)
    if risk_distance <= 0 or portfolio_value != portfolio_value:
        return 0
    shares = int((portfolio_value * risk_per_trade) / risk_distance)
    if shares <= 0:
        return 0
    if cost_fn is None:
        return shares
    # Ensure we can afford the position
    cost = shares * entry_price + cost_fn(shares, entry_price, sell=sell)
    while cost > portfolio_value and shares > 0:
        shares -= 1
        cost = shares * entry_price + cost_fn(shares, entry_price, sell=sell)
    return shares


# ═══════════════════════════════════════════════════════════════
# Backtest engine
# ═══════════════════════════════════════════════════════════════

BacktestParams = dict[str, int | float | str]

BacktestResult = dict[str, any]


def run_backtest(
    conn,
    tickers: list[str],
    market_days: list[datetime.date],
    slippage: float = 0.0001,
    risk_per_trade: float = 0.01,
    best_n_stocks: int = 5,
    atr_distance: float = 0.1,
    relative_volume_threshold: float = 2.0,
    single_stock_mode: bool = False,
) -> BacktestResult:
    """Run the opening-range-breakout strategy.

    In single-stock mode the entire AUM is traded on the single ticker
    (best_n_stocks is ignored).  In multi-stock mode the top N tickers
    by relative volume compete and at most one trade is opened per day.

    Returns a dict with equity, trade_fees, trade_count, total_fees,
    and the hyper-parameters used.
    """
    params: BacktestParams = {
        "slippage": slippage,
        "risk_per_trade": risk_per_trade,
        "best_n_stocks": best_n_stocks if not single_stock_mode else 1,
        "atr_distance": atr_distance,
        "relative_volume_threshold": relative_volume_threshold,
    }

    portfolio_value = 1_000.0
    trade_count = 0
    total_fees = 0.0
    trade_fees: list[dict] = []
    equity: list[dict] = []
    last_month: int | None = None

    for day in market_days:
        # ── Rank candidates by opening-range relative volume ──
        candidates: list[tuple[str, float]] = []
        for ticker in tickers:
            first_bar = load_first_bar(conn, ticker, day)
            if first_bar is None:
                continue
            candidates.append((ticker, first_bar[5]))

        candidates.sort(key=lambda x: x[1], reverse=True)
        if not candidates:
            equity.append(_equity_row(day, portfolio_value, trade_count, total_fees))
            continue

        stocks_in_play = candidates[: params["best_n_stocks"]]
        trade_made_today = False

        for stock_ticker, rel_vol in stocks_in_play:
            if rel_vol < relative_volume_threshold:
                continue

            # ── Load data ──
            first_bar = load_first_bar(conn, stock_ticker, day)
            daily_row = load_daily_row(conn, stock_ticker, day)
            intraday = load_intraday_bars(conn, stock_ticker, day)

            if not first_bar or not daily_row or not intraday or len(intraday) < 3:
                continue

            atr_value = daily_row[0]
            direction = compute_signal(first_bar)
            if direction == 0:
                continue

            entry_stop = compute_entry_stop(first_bar, atr_value, atr_distance, direction)
            if entry_stop is None:
                continue

            entry_price, stop_price = entry_stop

            # ── Walk through intraday candles ──
            is_open = False
            current_shares = 0

            for candle in intraday[1:-1]:
                low, high = candle[3], candle[2]

                if not is_open:
                    if trade_made_today and not single_stock_mode:
                        break  # only one trade/day in multi mode
                    triggered = False
                    if direction == -1 and low <= entry_price:
                        ep = entry_price * (1 - slippage)
                        triggered = True
                    elif direction == 1 and high >= entry_price:
                        ep = entry_price * (1 + slippage)
                        triggered = True
                    else:
                        continue

                    shares = compute_shares(
                        portfolio_value, risk_per_trade, ep, stop_price,
                        cost_fn=trade_cost, sell=(direction == -1),
                    )
                    if shares == 0:
                        continue

                    # Check affordability for long
                    if direction == 1:
                        cost = shares * ep + trade_cost(shares, ep, sell=False)
                        if cost > portfolio_value:
                            continue
                        portfolio_value -= cost
                    else:
                        portfolio_value += shares * ep - trade_cost(shares, ep, sell=True)

                    fee = trade_cost(shares, ep, sell=(direction == -1))
                    trade_count += 1
                    total_fees += fee
                    trade_fees.append({"date": day, "fee": fee})
                    is_open = True
                    current_shares = shares
                    trade_made_today = True

                else:
                    # Check stop
                    if direction == -1 and high >= stop_price:
                        fee = trade_cost(current_shares, stop_price, sell=False)
                        portfolio_value -= current_shares * stop_price + fee
                        total_fees += fee
                        trade_fees.append({"date": day, "fee": fee})
                        is_open = False
                    elif direction == 1 and low <= stop_price:
                        fee = trade_cost(current_shares, stop_price, sell=True)
                        portfolio_value += current_shares * stop_price - fee
                        total_fees += fee
                        trade_fees.append({"date": day, "fee": fee})
                        is_open = False

            # ── Close at last candle if still open ──
            if is_open:
                last_close = intraday[-1][4]
                if direction == -1:
                    fee = trade_cost(current_shares, last_close, sell=False)
                    portfolio_value -= current_shares * last_close + fee
                else:
                    fee = trade_cost(current_shares, last_close, sell=True)
                    portfolio_value += current_shares * last_close - fee
                total_fees += fee
                trade_fees.append({"date": day, "fee": fee})
                is_open = False

        # ── End of day bookkeeping ──
        equity.append(_equity_row(day, portfolio_value, trade_count, total_fees))

        if last_month is not None and day.month != last_month:
            portfolio_value -= MONTHLY_DATA_FEE
        last_month = day.month

    return {
        "equity": equity,
        "trade_fees": trade_fees,
        "trade_count": trade_count,
        "total_fees": total_fees,
        "params": params,
    }


def _equity_row(day, pv, tc, tf):
    return {"date": day, "portfolio_value": round(pv, 2), "trade_count": tc, "total_fees": round(tf, 2)}


# ═══════════════════════════════════════════════════════════════
# Report generation
# ═══════════════════════════════════════════════════════════════

def generate_returns(equity: list[dict]) -> pd.Series:
    """Build a daily returns Series from the equity tracker."""
    df = pd.DataFrame(equity)
    return (
        df.set_index(pd.to_datetime(df["date"]))["portfolio_value"]
        .pct_change()
        .dropna()
    )


def make_title(params: BacktestParams, ticker_count: int, n_total: int | None = None) -> str:
    """Build a tearsheet title from hyper-parameters."""
    parts = [
        f"rv={params['relative_volume_threshold']}",
        f"atr={params['atr_distance']}",
        f"n={params['best_n_stocks']}",
        f"risk={params['risk_per_trade']}",
        f"fee={FEE_PER_SHARE_TOTAL}",
        f"min={MINIMUM_PER_ORDER}",
    ]
    ticker_info = f"{ticker_count}/{n_total}" if n_total else str(ticker_count)
    parts.append(f"({ticker_info} tickers)")
    return " ".join(parts)


def generate_tearsheet(
    returns: pd.Series,
    output_dir: str | Path,
    title: str,
    filename: str | None = None,
) -> str:
    """Generate a quantstats HTML tearsheet, return the output path."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if filename is None:
        filename = f"{uuid.uuid7()}.html"
    path = output_dir / filename
    qs.reports.html(returns, output=str(path), title=title)
    return str(path)


def generate_fee_chart(
    trade_fees: list[dict],
    trade_count: int,
    total_fees: float,
    output_dir: str | Path | None = None,
) -> plt.Figure | None:
    """Plot cumulative fees + distribution. Returns the figure or None."""
    if not trade_fees:
        return None
    fee_df = pd.DataFrame(trade_fees)
    fee_df["date"] = pd.to_datetime(fee_df["date"])
    fee_df = fee_df.sort_values("date")
    fee_df["cumulative"] = fee_df["fee"].cumsum()

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 4))

    ax1.plot(fee_df["date"], fee_df["cumulative"])
    ax1.set_title(f"Cumulative Fees — {trade_count} trades, ${total_fees:.2f} total")
    ax1.set_ylabel("Cumulative fee ($)")
    ax1.set_xlabel("Date")
    ax1.grid(True, alpha=0.3)

    ax2.hist(fee_df["fee"], bins=50, edgecolor="white")
    ax2.set_title("Fee Distribution per Trade Side")
    ax2.set_xlabel("Fee ($)")
    ax2.set_ylabel("Frequency")
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()

    if output_dir:
        path = Path(output_dir) / f"fees_{uuid.uuid7()}.png"
        fig.savefig(path, dpi=150, bbox_inches="tight")
        import logging
        logging.getLogger("ibkr_data.backtest").info("Fee chart saved to %s", path)

    return fig


# ═══════════════════════════════════════════════════════════════
# Single-stock runner
# ═══════════════════════════════════════════════════════════════

def _max_drawdown(returns: pd.Series) -> float:
    """Compute max drawdown from a return series."""
    cumulative = (1 + returns).cumprod()
    peak = cumulative.expanding().max()
    dd = (cumulative - peak) / peak
    return float(dd.min())


def run_single_stock_backtest(
    conn,
    ticker: str,
    market_days: list[datetime.date],
    **kwargs,
) -> BacktestResult:
    """Run the strategy on a single ticker and save tearsheet + DB results."""
    result = run_backtest(
        conn,
        tickers=[ticker],
        market_days=market_days,
        single_stock_mode=True,
        **kwargs,
    )
    return result


def save_single_stock_results(
    conn,
    ticker: str,
    result: BacktestResult,
    output_dir: str | Path = "backtests/single",
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
):
    """Generate and save tearsheet + fee chart for a single stock.

    Also writes performance metrics into the tickers table.
    """
    returns = generate_returns(result["equity"])
    title_parts = [
        f"rv={result['params']['relative_volume_threshold']}",
        f"atr={result['params']['atr_distance']}",
        f"risk={result['params']['risk_per_trade']}",
        f"fee={FEE_PER_SHARE_TOTAL}",
        f"min={MINIMUM_PER_ORDER}",
    ]
    title = f"{ticker} {' '.join(title_parts)}"

    if start_date and end_date:
        filename = f"{ticker}_{start_date}_{end_date}.html"
    else:
        filename = f"{ticker}.html"
    generate_tearsheet(returns, output_dir=output_dir, title=title, filename=filename)
    fig = generate_fee_chart(
        result["trade_fees"], result["trade_count"], result["total_fees"],
    )
    if fig is not None:
        plt.close(fig)

    # ── Compute metrics ──
    total_return = float(returns.add(1).prod() - 1)
    sharpe = float(qs.stats.sharpe(returns).iloc[0])
    max_dd = float(_max_drawdown(returns))

    # ── Persist to tickers table ──
    _ensure_backtest_columns(conn)
    conn.execute(
        """
        UPDATE tickers SET
            backtest_sharpe = ?,
            backtest_total_return = ?,
            backtest_max_drawdown = ?,
            backtest_trade_count = ?,
            backtest_total_fees = ?
        WHERE symbol = ?
        """,
        (sharpe, total_return, max_dd, result["trade_count"], result["total_fees"], ticker.upper()),
    )
    conn.commit()


def _ensure_backtest_columns(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tickers)")}
    for col_name, col_type in [
        ("backtest_sharpe", "REAL"),
        ("backtest_total_return", "REAL"),
        ("backtest_max_drawdown", "REAL"),
        ("backtest_trade_count", "INTEGER"),
        ("backtest_total_fees", "REAL"),
    ]:
        if col_name not in cols:
            conn.execute(f"ALTER TABLE tickers ADD COLUMN {col_name} {col_type}")
