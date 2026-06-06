import argparse
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf
import polygon

logger = logging.getLogger("ibkr_data.compare_yf")

COLUMNS = ["open", "high", "low", "close", "volume"]

POLYGON_RATE = 5.0  # requests per minute on free tier


def _load_csv_5min(ticker: str, data_dir: Path, since: datetime, csv_name: str) -> pd.DataFrame:
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
                csv_file = day_dir / csv_name
                if csv_file.exists():
                    df = pd.read_csv(csv_file, parse_dates=["date"])
                    df = df[df["date"] >= since]
                    if not df.empty:
                        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    return df.sort_values("date").reset_index(drop=True)


def _fetch_polygon_day(client: polygon.RESTClient, ticker: str, day: datetime) -> pd.DataFrame | None:
    date_str = day.strftime("%Y-%m-%d")
    try:
        aggs = client.get_aggs(ticker, 5, "minute", date_str, date_str)
    except Exception as e:
        logger.warning("Polygon error for %s on %s: %s", ticker, date_str, e)
        return None
    if not aggs:
        return None
    rows = []
    for a in aggs:
        ts = datetime.fromtimestamp(a.timestamp / 1000, tz=timezone.utc)
        rows.append({
            "date": ts,
            "open": a.open,
            "high": a.high,
            "low": a.low,
            "close": a.close,
            "volume": float(a.volume),
        })
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    return df.sort_values("date").reset_index(drop=True)


def _save_polygon(poly_df: pd.DataFrame, ticker: str, data_dir: Path, day: datetime):
    out_dir = data_dir / ticker / str(day.year) / f"{day.month:02d}" / f"{day.day:02d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "polygon.csv"
    poly_df.to_csv(out_path, index=False)


def _report_three(ibkr: pd.DataFrame, yf_df: pd.DataFrame, poly_df: pd.DataFrame, ticker: str):
    for df in [ibkr, yf_df, poly_df]:
        df["date"] = pd.to_datetime(df["date"], utc=True)

    merged = ibkr.merge(yf_df, on="date", suffixes=("_ibkr", "_yf"), how="inner")
    if merged.empty:
        logger.warning("No overlapping bars between IBKR and yfinance for %s", ticker)
        return

    merged = merged.merge(poly_df, on="date", suffixes=("", "_poly"), how="inner")
    if merged.empty:
        logger.warning("No overlapping bars across all three sources for %s", ticker)
        return

    # rename polygon columns
    for col in COLUMNS:
        if col != "date":
            merged = merged.rename(columns={col: f"{col}_poly"})

    logger.info("Overlap: %d bars (%s to %s)", len(merged), merged["date"].min(), merged["date"].max())

    def _row(col_name: str, ib: pd.Series, yf_vals: pd.Series, po: pd.Series):
        diff_ib_yf = ib - yf_vals
        diff_yf_po = yf_vals - po
        diff_ib_po = ib - po

        if col_name == "volume":
            fmt = lambda v: f"{v:>14,.0f}"
            fmt_f = lambda v: f"{v:>14,.0f}"
        else:
            fmt = lambda v: f"{v:>14.2f}"
            fmt_f = lambda v: f"{v:>14.2f}"

        parts = [
            f"{col_name:<8}",
            fmt(ib.mean()),
            fmt(ib.std()),
            fmt(yf_vals.mean()),
            fmt(yf_vals.std()),
            fmt(po.mean()),
            fmt(po.std()),
            fmt_f(diff_ib_yf.mean()),
            fmt_f(diff_yf_po.mean()),
            fmt_f(diff_ib_po.mean()),
        ]
        return "".join(parts)

    print()
    print(f"{'='*130}")
    print(f"  5-MIN CANDLE COMPARISON  —  {ticker}  (IBKR / yfinance / Polygon)")
    print(f"{'='*130}")
    hdr = f"{'Column':<8} {'IBKR mean':>14} {'IBKR std':>14} {'YF mean':>14} {'YF std':>14} {'Poly mean':>14} {'Poly std':>14} {'IBKR-YF Δ':>14} {'YF-Poly Δ':>14} {'IBKR-Poly Δ':>14}"
    print(hdr)
    print("-" * len(hdr))

    for col in COLUMNS:
        ib = merged[f"{col}_ibkr"]
        yf_vals = merged[f"{col}_yf"]
        po = merged[f"{col}_poly"]
        print(_row(col, ib, yf_vals, po))

    print(f"{'='*130}")
    ib_yf_c = (merged["close_ibkr"] - merged["close_yf"]).abs()
    yf_po_c = (merged["close_yf"] - merged["close_poly"]).abs()
    ib_po_c = (merged["close_ibkr"] - merged["close_poly"]).abs()
    print(f"  Close MAE:      IBKR–YF=${ib_yf_c.mean():.4f}   YF–Poly=${yf_po_c.mean():.4f}   IBKR–Poly=${ib_po_c.mean():.4f}")
    print(f"  Close max |Δ|:  IBKR–YF=${ib_yf_c.max():.4f}   YF–Poly=${yf_po_c.max():.4f}   IBKR–Poly=${ib_po_c.max():.4f}")

    ibkr_poly_ratio = merged["volume_ibkr"].sum() / merged["volume_poly"].sum()
    yf_poly_ratio = merged["volume_yf"].sum() / merged["volume_poly"].sum()
    print(f"  Total volume:   IBKR={merged['volume_ibkr'].sum():,.0f}  YF={merged['volume_yf'].sum():,.0f}  Poly={merged['volume_poly'].sum():,.0f}")
    print(f"  Volume ratio:   IBKR/Poly={ibkr_poly_ratio:.4f}  YF/Poly={yf_poly_ratio:.4f}")

    print(f"{'='*130}")
    print()


def fetch_and_compare(ticker: str, data_dir: Path, polygon_key: str | None):
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
    until = yf_raw["date"].max()
    logger.info("yfinance data range: %s to %s", since, until)

    # save yfinance daily files
    saved = 0
    for (year, month, day), group in yf_raw.groupby([yf_raw["date"].dt.year, yf_raw["date"].dt.month, yf_raw["date"].dt.day]):
        out_dir = data_dir / ticker / str(year) / f"{month:02d}" / f"{day:02d}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "yf.csv"
        group.to_csv(out_path, index=False)
        saved += 1
    logger.info("Saved %d daily yf 5-min files", saved)

    # load IBKR data
    ibkr = _load_csv_5min(ticker, data_dir, since, "ibkr.csv")
    if ibkr.empty:
        logger.warning("No IBKR 5-min data found for comparison")
        return
    logger.info("IBKR 5-min data range: %s to %s", ibkr["date"].min(), ibkr["date"].max())

    # fetch Polygon data if key provided
    poly_dfs = []
    if polygon_key:
        client = polygon.RESTClient(polygon_key)
        day = since.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = until.replace(hour=0, minute=0, second=0, microsecond=0)
        total_days = (end_day - day).days + 1

        # check for cached polygon files first
        need_days = []
        for i in range(total_days):
            d = day + timedelta(days=i)
            out_dir = data_dir / ticker / str(d.year) / f"{d.month:02d}" / f"{d.day:02d}"
            csv_path = out_dir / "polygon.csv"
            if csv_path.exists():
                df = pd.read_csv(csv_path, parse_dates=["date"])
                if not df.empty:
                    poly_dfs.append(df)
                    continue
            need_days.append(d)

        if need_days:
            logger.info("Fetching %d days from Polygon API (rate limited to %s req/min)...", len(need_days), POLYGON_RATE)
            for i, d in enumerate(need_days):
                if i > 0:
                    time.sleep(60.0 / POLYGON_RATE)
                logger.debug("  Polygon %s day %d/%d", d.strftime("%Y-%m-%d"), i + 1, len(need_days))
                df = _fetch_polygon_day(client, ticker, d)
                if df is not None and not df.empty:
                    _save_polygon(df, ticker, data_dir, d)
                    poly_dfs.append(df)

        if poly_dfs:
            poly_all = pd.concat(poly_dfs, ignore_index=True)
            poly_all = poly_all.sort_values("date").reset_index(drop=True)
            logger.info("Polygon 5-min data range: %s to %s", poly_all["date"].min(), poly_all["date"].max())
            _report_three(ibkr, yf_raw, poly_all, ticker)
        else:
            logger.warning("No Polygon data fetched")
    else:
        # no polygon key — include a placeholder row so the report still shows IBKR vs YF
        dummy = ibkr.copy()
        for col in COLUMNS:
            if col != "date":
                dummy[col] = float("nan")
        _report_three(ibkr, yf_raw, dummy, ticker)


def main():
    parser = argparse.ArgumentParser(description="Compare 5-min candles: IBKR vs yfinance vs Polygon")
    parser.add_argument("--ticker", default="TSLA")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--polygon-key", help="Polygon.io API key (enables third-source comparison)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    fetch_and_compare(args.ticker, Path(args.data_dir), args.polygon_key)


if __name__ == "__main__":
    main()
