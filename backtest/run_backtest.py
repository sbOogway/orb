"""
Multi-stock Opening Range Breakout backtest using Nautilus Trader.

Scans a universe of stocks each day, picks the top N by relative volume,
and trades only those. Position sizing is 1% risk per trade with a 10% ATR stop.
"""

import argparse
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger("backtest")

from db import get_connection, load_ticker_df

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.model.data import Bar, BarType, BarSpecification
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue, ClientOrderId
from nautilus_trader.model.enums import (
    BarAggregation,
    PriceType,
    OrderSide,
    TimeInForce,
    AccountType,
    OmsType,
)
from nautilus_trader.model.objects import Price, Quantity, Money
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.currencies import USD
from nautilus_trader.core.rust.model import TriggerType
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.orders import MarketOrder, StopMarketOrder


# ---------------------------------------------------------------------------
#  Data loading
# ---------------------------------------------------------------------------

def load_ticker_bars_csv(ticker: str, data_dir: Path) -> pd.DataFrame:
    """Load all per-day ibkr.csv files for a ticker into a single DataFrame."""
    ticker_dir = data_dir / ticker
    if not ticker_dir.is_dir():
        logger.warning("No data directory for %s at %s", ticker, ticker_dir)
        return pd.DataFrame()
    rows = []
    for year_dir in sorted(ticker_dir.iterdir()):
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir():
                continue
            for day_dir in sorted(month_dir.iterdir()):
                csv_path = day_dir / "ibkr.csv"
                if csv_path.exists():
                    d = pd.read_csv(csv_path)
                    rows.append(d)
    if not rows:
        return pd.DataFrame()
    df = pd.concat(rows, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.sort_values("date").reset_index(drop=True)
    return df


def load_ticker_bars_db(ticker: str, conn, start: str | None, end: str | None) -> pd.DataFrame:
    return load_ticker_df(conn, ticker, source="ibkr", timeframe="5m", start=start, end=end)


def bars_to_nautilus(df: pd.DataFrame, bar_type: BarType) -> list[Bar]:
    """Convert a DataFrame of IBKR 5-min bars to Nautilus Bar objects.

    Expected columns: date, open, high, low, close, volume
    """
    bars = []
    for _, row in df.iterrows():
        ts = int(row["date"].timestamp() * 1_000_000_000)
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price.from_str(f"{row['open']:.2f}"),
                high=Price.from_str(f"{row['high']:.2f}"),
                low=Price.from_str(f"{row['low']:.2f}"),
                close=Price.from_str(f"{row['close']:.2f}"),
                volume=Quantity.from_int(int(row["volume"])),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return bars


# ---------------------------------------------------------------------------
#  Per-ticker state
# ---------------------------------------------------------------------------

@dataclass
class TickerState:
    symbol: str
    instrument_id: InstrumentId
    bar_type: BarType

    # day accumulation (rolling 30 days)
    day_bars: dict[str, list[Bar]] = field(default_factory=dict)
    day_keys: list[str] = field(default_factory=list)

    # opening bar for current day
    opening_bar: Bar | None = None
    has_opening: bool = False

    # order tracking
    entry_submitted: bool = False
    pending_sl_bar_type = None
    pending_sl_side = None
    pending_sl_shares = 0
    pending_sl_price = 0.0
    pending_sl_day_key = ""

    # cached computed values for the current day's ranking
    rel_vol: float = 0.0
    avg_daily_volume: float = 0.0
    atr_14: float = 0.0


# ---------------------------------------------------------------------------
#  Strategy
# ---------------------------------------------------------------------------

class MultiORB(Strategy):

    def __init__(self, tickers: list[str], top_n: int = 1, aum: float = 1_000_000):
        super().__init__()
        self._tickers = tickers
        self._top_n = top_n
        self._aum = aum

        self._states: dict[str, TickerState] = {}
        self._current_day: str | None = None
        self._ranked_today: bool = False
        self._open_entry_orders: set[str] = set()  # instrument IDs with pending GTC entries

    def on_start(self):
        for sym in self._tickers:
            iid = InstrumentId(Symbol(sym), Venue("SMART"))
            bt = BarType(iid, BarSpecification(5, BarAggregation.MINUTE, PriceType.LAST))
            self.subscribe_bars(bt)
            self._states[sym] = TickerState(symbol=sym, instrument_id=iid, bar_type=bt)
        self.log.info(f"MultiORB started with {len(self._tickers)} tickers, top_n={self._top_n}")

    # ------------------------------------------------------------------
    #  Bar handler
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar):
        ticker = bar.bar_type.instrument_id.symbol.value
        st = self._states.get(ticker)
        if st is None:
            return

        bar_dt = datetime.fromtimestamp(bar.ts_event / 1_000_000_000, tz=timezone.utc)
        day_key = bar_dt.strftime("%Y-%m-%d")

        # ---- detect new trading day ----
        if day_key != self._current_day:
            self._current_day = day_key
            self._ranked_today = False
            for s in self._states.values():
                s.has_opening = False
                s.opening_bar = None
                s.entry_submitted = False

        # ---- accumulate bars (rolling 30 days) ----
        if day_key not in st.day_bars:
            st.day_bars[day_key] = []
            st.day_keys.append(day_key)
            if len(st.day_keys) > 30:
                old = st.day_keys.pop(0)
                st.day_bars.pop(old, None)
        st.day_bars[day_key].append(bar)

        # ---- detect opening bar (first bar of day for this ticker) ----
        if not st.has_opening and day_key == self._current_day:
            st.has_opening = True
            st.opening_bar = bar

        # ---- once we have all opening bars, rank and trade ----
        if not self._ranked_today:
            all_in = all(s.has_opening for s in self._states.values())
            # also rank if past the opening window (13:35+ UTC)
            past_open = bar_dt.hour > 13 or (bar_dt.hour == 13 and bar_dt.minute > 30)
            if all_in or past_open:
                self._rank_and_trade(day_key)
                self._ranked_today = True

        # ---- end-of-day flatten ----
        if bar_dt.hour == 19 and bar_dt.minute == 55:
            self._flatten_all(bar)

    # ------------------------------------------------------------------
    #  Ranking & entry
    # ------------------------------------------------------------------

    def _compute_indicators(self, st: TickerState, day_key: str) -> bool:
        """Compute RelVol, avg_daily_volume, ATR(14) for one ticker.

        Returns True if all filters pass and the stock is tradeable.
        """
        ob = st.opening_bar
        if ob is None:
            return False

        open_price = float(ob.open.as_double())
        close_price = float(ob.close.as_double())
        volume_5m = int(ob.volume.as_double())

        # Filter 1: price > $5
        if open_price <= 5.0:
            return False

        # Need at least 14 prior days for ATR / volume
        curr_idx = st.day_keys.index(day_key)
        if curr_idx < 14:
            return False

        prior_keys = st.day_keys[max(0, curr_idx - 14):curr_idx]

        # Build daily aggregates from 5-min bars
        daily_ohlc = {}
        day_volumes = []
        prior_open_volumes = []
        for pk in prior_keys:
            pk_bars = st.day_bars.get(pk, [])
            if not pk_bars:
                continue
            daily_high = max(float(b.high.as_double()) for b in pk_bars)
            daily_low = min(float(b.low.as_double()) for b in pk_bars)
            daily_close = float(pk_bars[-1].close.as_double())
            daily_vol = sum(int(b.volume.as_double()) for b in pk_bars)
            daily_ohlc[pk] = {"high": daily_high, "low": daily_low, "close": daily_close}
            day_volumes.append(daily_vol)
            prior_open_volumes.append(int(pk_bars[0].volume.as_double()))

        if not day_volumes:
            return False

        avg_daily_volume = sum(day_volumes) / len(day_volumes)

        # Filter 2: avg daily volume >= 1M
        if avg_daily_volume < 1_000_000:
            return False

        # ATR(14) from daily candles
        dkeys = sorted(daily_ohlc.keys())
        tr_values = []
        for i, dk in enumerate(dkeys):
            d = daily_ohlc[dk]
            h, l, c = d["high"], d["low"], d["close"]
            if i == 0:
                tr = h - l
            else:
                prev_c = daily_ohlc[dkeys[i - 1]]["close"]
                tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            tr_values.append(tr)
        atr_14 = sum(tr_values[-14:]) / min(len(tr_values), 14) if tr_values else 0

        # Filter 3: ATR > $0.50
        if atr_14 <= 0.50:
            return False

        # Relative volume (opening bar)
        avg_open_vol = sum(prior_open_volumes) / len(prior_open_volumes) if prior_open_volumes else 0
        rel_vol = volume_5m / avg_open_vol if avg_open_vol > 0 else 0

        # Filter 4: RelVol >= 100%
        if rel_vol < 1.0:
            return False

        # Store computed values for ranking
        st.rel_vol = rel_vol
        st.avg_daily_volume = avg_daily_volume
        st.atr_14 = atr_14
        return True

    def _rank_and_trade(self, day_key: str):
        """Compute indicators for tickers with opening bars, rank by RelVol, trade top N."""
        candidates = []
        for sym, st in self._states.items():
            if st.opening_bar is None:
                continue
            # skip tickers that already have a position or a pending entry
            if self.portfolio is not None:
                if not self.portfolio.is_flat(st.instrument_id):
                    self.log.debug(f"{day_key} {sym}: skip (open position)")
                    continue
            if sym in self._open_entry_orders:
                self.log.debug(f"{day_key} {sym}: skip (pending entry)")
                continue
            if self._compute_indicators(st, day_key):
                candidates.append((st.rel_vol, sym, st))
            else:
                self.log.debug(f"{day_key} {sym}: filtered out")

        # Sort by RelVol descending, take top N
        candidates.sort(key=lambda x: -x[0])
        selected = candidates[:self._top_n]

        if not selected:
            self.log.debug(f"{day_key}: no candidates passed filters")
            return

        self.log.info(
            f"{day_key} RANK | selected={','.join(s for _, s, _ in selected)} | top_relvol={selected[0][0] * 100:.2f}%"
        )

        # Trade each selected stock
        for _, sym, st in selected:
            self._submit_entry(st, day_key)

    def _submit_entry(self, st: TickerState, day_key: str):
        """Determine direction and submit a stop entry order."""
        ob = st.opening_bar
        ts_str = datetime.now(timezone.utc).strftime("%H%M%S%f")
        open_price = float(ob.open.as_double())
        close_price = float(ob.close.as_double())
        high = float(ob.high.as_double())
        low = float(ob.low.as_double())

        direction = "long" if close_price >= open_price else "short"

        # Position sizing: risk 1% of current equity at 10% ATR
        current_equity = self._aum
        if self.portfolio is not None:
            acct = self.portfolio.account(venue=st.instrument_id.venue)
            if acct is not None:
                bal = acct.balance(USD)
                if bal is not None:
                    current_equity = bal.total.as_double()

        risk_per_share = st.atr_14 * 0.10
        if risk_per_share <= 0:
            return
        max_loss = current_equity * 0.01
        shares = int(max_loss / risk_per_share)
        if shares <= 0:
            return

        entry_price = high if direction == "long" else low
        position_value = shares * entry_price
        if position_value > 4 * current_equity:
            shares = int(4 * current_equity / entry_price)
            if shares <= 0:
                return

        stop_loss_price = entry_price - risk_per_share if direction == "long" else entry_price + risk_per_share

        self.log.info(
            f"{day_key} {st.symbol} TRADE | dir={direction} entry={entry_price:.2f} stop={stop_loss_price:.2f} "
            f"shares={shares} ATR={st.atr_14:.2f} RelVol={st.rel_vol * 100:.2f}% "
            f"avg_daily_vol={st.avg_daily_volume:.0f}"
        )

        # Submit stop entry (no stop-loss yet — submitted on fill)
        init_id = UUID4()
        if direction == "long":
            entry = StopMarketOrder(
                trader_id=self.trader_id,
                strategy_id=self.id,
                instrument_id=st.instrument_id,
                client_order_id=ClientOrderId(f"buy-{st.symbol}-{day_key}-{ts_str}"),
                order_side=OrderSide.BUY,
                quantity=Quantity.from_int(shares),
                trigger_price=Price.from_str(f"{entry_price:.2f}"),
                trigger_type=TriggerType.DEFAULT,
                init_id=init_id,
                ts_init=0,
                time_in_force=TimeInForce.GTC,
            )
            sl_side = OrderSide.SELL
        else:
            entry = StopMarketOrder(
                trader_id=self.trader_id,
                strategy_id=self.id,
                instrument_id=st.instrument_id,
                client_order_id=ClientOrderId(f"sell-{st.symbol}-{day_key}-{ts_str}"),
                order_side=OrderSide.SELL,
                quantity=Quantity.from_int(shares),
                trigger_price=Price.from_str(f"{entry_price:.2f}"),
                trigger_type=TriggerType.DEFAULT,
                init_id=init_id,
                ts_init=0,
                time_in_force=TimeInForce.GTC,
            )
            sl_side = OrderSide.BUY

        self.submit_order(entry)
        st.entry_submitted = True
        self._open_entry_orders.add(st.symbol)

        # stash stop-loss params for after entry fills
        st.pending_sl_shares = shares
        st.pending_sl_price = stop_loss_price
        st.pending_sl_side = sl_side
        st.pending_sl_day_key = day_key

    # ------------------------------------------------------------------
    #  Fill handler — submits stop-loss
    # ------------------------------------------------------------------

    def on_order_filled(self, order):
        self.log.info(
            f"FILLED | {order.order_side.name} {order.last_qty} {order.instrument_id.symbol} @ {float(order.last_px):.2f}"
        )
        sym = order.instrument_id.symbol.value
        st = self._states.get(sym)

        # track entry orders
        cid = order.client_order_id.value
        if cid.startswith("buy-") or cid.startswith("sell-"):
            self._open_entry_orders.discard(sym)

        if st is None or st.pending_sl_shares == 0:
            return

        ts_str = datetime.now(timezone.utc).strftime("%H%M%S%f")
        sl = StopMarketOrder(
            trader_id=self.trader_id,
            strategy_id=self.id,
            instrument_id=st.instrument_id,
            client_order_id=ClientOrderId(f"sl-{st.symbol}-{st.pending_sl_day_key}-{ts_str}"),
            order_side=st.pending_sl_side,
            quantity=Quantity.from_int(st.pending_sl_shares),
            trigger_price=Price.from_str(f"{st.pending_sl_price:.2f}"),
            trigger_type=TriggerType.DEFAULT,
            init_id=UUID4(),
            ts_init=0,
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
        )
        self.submit_order(sl)
        st.pending_sl_shares = 0

    def on_order_rejected(self, order):
        self.log.warning(f"REJECTED | {order}")
        sym = order.instrument_id.symbol.value
        cid = order.client_order_id.value
        # if a pending entry is rejected, clear the tracker so we can retry
        if cid.startswith("buy-") or cid.startswith("sell-"):
            self._open_entry_orders.discard(sym)
        # if a stop-loss is rejected, close the position at market
        if cid.startswith("sl-"):
            st = self._states.get(sym)
            if st is not None and self.portfolio is not None:
                qty = self.portfolio.net_position(st.instrument_id)
                if qty != 0:
                    side = OrderSide.SELL if qty > 0 else OrderSide.BUY
                    close = MarketOrder(
                        trader_id=self.trader_id,
                        strategy_id=self.id,
                        instrument_id=st.instrument_id,
                        client_order_id=ClientOrderId(f"sl-fail-{sym}-{datetime.now(timezone.utc).strftime('%H%M%S%f')}"),
                        order_side=side,
                        quantity=Quantity.from_int(abs(int(qty))),
                        init_id=UUID4(),
                        ts_init=0,
                        time_in_force=TimeInForce.GTC,
                        reduce_only=True,
                    )
                    self.submit_order(close)
                    self.log.warning(f"SL rejected for {sym}, closing position @ market")

    # ------------------------------------------------------------------
    #  End-of-day flatten
    # ------------------------------------------------------------------

    def _flatten_all(self, bar: Bar):
        """Flatten all open positions at market."""
        if self.portfolio is None:
            return
        for sym, st in self._states.items():
            if self.portfolio.is_flat(st.instrument_id):
                continue
            qty = self.portfolio.net_position(st.instrument_id)
            side = OrderSide.SELL if qty > 0 else OrderSide.BUY
            abs_qty = Quantity.from_int(abs(int(qty)))
            order = MarketOrder(
                trader_id=self.trader_id,
                strategy_id=self.id,
                instrument_id=st.instrument_id,
                client_order_id=ClientOrderId(f"eod-{sym}-{datetime.now(timezone.utc).strftime('%H%M%S%f')}"),
                order_side=side,
                quantity=abs_qty,
                init_id=UUID4(),
                ts_init=0,
                time_in_force=TimeInForce.GTC,
                reduce_only=True,
            )
            self.submit_order(order)
            self.log.info(f"EOD flatten {sym}: {side.name} {abs_qty} @ market")

    def on_stop(self):
        self.log.info("MultiORB stopped")


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Multi-stock Opening Range Breakout backtest",
    )
    parser.add_argument("--tickers", default=None, help="Comma-separated ticker symbols (default: read from DB tickers table when --db is used)")
    parser.add_argument("--top-n", type=int, default=1, help="Top N stocks by RelVol to trade each day")
    parser.add_argument("--data-dir", default="../data")
    parser.add_argument("--db", default=None, nargs="?", const="", help="Load from SQLite DB instead of CSV files")
    parser.add_argument("--aum", type=float, default=1_000_000, help="Starting AUM")
    parser.add_argument("--start", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", help="End date YYYY-MM-DD")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")

    data_dir = Path(args.data_dir)

    # ---- determine data source & tickers ----
    use_db = args.db is not None
    db_conn = None
    if use_db:
        db_path = args.db if args.db else None
        db_conn = get_connection(db_path)
        logger.info("Using SQLite DB: %s", db_conn.execute("PRAGMA database_list").fetchone()[2])
        load_fn = lambda sym: load_ticker_bars_db(sym, db_conn, args.start, args.end)
    else:
        load_fn = lambda sym: load_ticker_bars_csv(sym, data_dir)

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]
    elif use_db:
        from db import get_tickers, ensure_tickers_table
        ensure_tickers_table(db_conn)
        tickers = get_tickers(db_conn)
        logger.info("Reading %d tickers from DB", len(tickers))
    else:
        tickers = ["TSLA"]

    logger.info("Loading data for %d tickers: %s", len(tickers), tickers)

    # ---- load all ticker data ----
    all_bars: list[Bar] = []
    instruments = []
    for sym in tickers:
        df = load_fn(sym)
        if df.empty:
            logger.warning("No data for %s, skipping", sym)
            continue
        if not use_db:
            if args.start:
                df = df[df["date"] >= args.start]
            if args.end:
                df = df[df["date"] <= args.end]
            if df.empty:
                logger.warning("No data for %s after date filter, skipping", sym)
                continue

        iid = InstrumentId(Symbol(sym), Venue("SMART"))
        bt = BarType(iid, BarSpecification(5, BarAggregation.MINUTE, PriceType.LAST))
        bars = bars_to_nautilus(df, bt)
        all_bars.extend(bars)

        from decimal import Decimal
        instr = Equity(
            iid,
            Symbol(sym),
            USD,
            2,
            Price.from_str("0.01"),
            Quantity.from_int(1),
            0, 0,
            max_quantity=Quantity.from_int(100_000_000),
            min_quantity=Quantity.from_int(1),
            margin_init=Decimal("0.25"),
            margin_maint=Decimal("0.10"),
            maker_fee=Decimal("0.0000125"),
            taker_fee=Decimal("0.0000125"),
        )
        instruments.append(instr)
        logger.info("  %s: %d bars (%s to %s)", sym, len(bars), df["date"].min(), df["date"].max())

    if not all_bars:
        logger.error("No data loaded for any ticker")
        return

    logger.info("Total bars across all tickers: %d", len(all_bars))

    # ---- backtest engine ----
    venue_name = Venue("SMART")
    engine = BacktestEngine()

    engine.add_venue(
        venue=venue_name,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(args.aum, USD)],
        base_currency=USD,
    )

    for instr in instruments:
        engine.add_instrument(instr)
    engine.add_data(all_bars)

    from stats import CalmarRatio, AnnualizedReturn, RunConfig
    engine.portfolio.analyzer.register_statistic(CalmarRatio())
    engine.portfolio.analyzer.register_statistic(AnnualizedReturn())
    engine.portfolio.analyzer.register_statistic(RunConfig(
        tickers=",".join(tickers),
        top_n=str(args.top_n),
        aum=f"${args.aum:,.0f}",
        start=args.start or "earliest",
        end=args.end or "latest",
        source="DB" if use_db else "CSV",
        strategy="MultiORB",
    ))

    strategy = MultiORB(tickers=[i.id.symbol.value for i in instruments], top_n=args.top_n, aum=args.aum)
    engine.add_strategy(strategy)

    logger.info("Running backtest ...")
    engine.run()

    # ---- report ----
    from report import print_report

    ticker_str = "+".join(tickers) if len(tickers) <= 6 else f"{len(tickers)}tickers"
    title = f"ORB {ticker_str} top-{args.top_n} | {args.aum/1e6:.0f}M AUM | {args.start or 'earliest'} to {args.end or 'latest'}"
    print_report(
        engine,
        venue_name,
        title=title,
    )

    engine.dispose()
    if db_conn:
        db_conn.close()


if __name__ == "__main__":
    main()
