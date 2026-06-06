"""HTML tearsheet report generator for multi-stock ORB backtest.

Mimics the approach from github.com/sbOogway/sbt:
- nautilus_trader Plotly tearsheet (equity, drawdown, monthly returns, etc.)
- Custom CSS-styled performance tables
- Positions, fills, orders, account DataFrames as HTML tables
"""

import webbrowser
from pathlib import Path

import pandas as pd
from nautilus_trader.analysis import create_tearsheet
from nautilus_trader.backtest.engine import BacktestEngine


def _df_to_html_table(df: pd.DataFrame) -> str:
    if not len(df):
        return "<p>None</p>"
    return df.to_html(
        classes="report-table",
        border=0,
        max_rows=50,
        escape=True,
        sparsify=False,
    )


def _build_reports_html(
    stats_pnls: dict,
    stats_returns: dict,
    stats_general: dict,
    positions_df: pd.DataFrame,
    fills_df: pd.DataFrame,
    orders_df: pd.DataFrame,
    account_df: pd.DataFrame,
) -> str:
    combined = {**stats_pnls, **stats_returns, **stats_general}
    stat_rows = "\n".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>"
        for k, v in combined.items()
    )

    return f"""
<style>
.report-section {{
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px;
  font-family: 'Segoe UI', system-ui, sans-serif;
  background: #f5f5f5;
  color: #222;
}}
.report-section h1 {{
  border-bottom: 2px solid #1976d2;
  padding-bottom: 8px;
}}
.report-section h2 {{
  margin-top: 28px;
}}
.table-wrap {{
  overflow-x: auto;
  margin-bottom: 24px;
  border-radius: 6px;
  border: 1px solid #ddd;
  background: #fff;
}}
.table-wrap table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  white-space: nowrap;
}}
.table-wrap th {{
  background: #1976d2;
  color: #fff;
  padding: 8px 10px;
  text-align: left;
  font-weight: 600;
}}
.table-wrap td {{
  padding: 6px 10px;
  border-bottom: 1px solid #eee;
}}
.table-wrap tr:hover td {{
  background: #f0f7ff;
}}
.stats-table {{
  width: auto;
  min-width: 400px;
  border-collapse: collapse;
  margin-bottom: 24px;
  background: #fff;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid #ddd;
}}
.stats-table td {{
  padding: 6px 16px;
  border-bottom: 1px solid #eee;
}}
.stats-table td:first-child {{
  font-weight: 600;
  color: #555;
}}
.stats-table td:last-child {{
  text-align: right;
  font-family: 'JetBrains Mono', 'Consolas', monospace;
}}
.stats-table tr:last-child td {{
  border-bottom: none;
}}
</style>
<div class="report-section">
  <h1>Backtest Report</h1>

  <h2>Portfolio Performance</h2>
  <table class="stats-table">
    {stat_rows}
  </table>

  <h2>Positions ({len(positions_df)})</h2>
  <div class="table-wrap">{_df_to_html_table(positions_df)}</div>

  <h2>Fills ({len(fills_df)})</h2>
  <div class="table-wrap">{_df_to_html_table(fills_df)}</div>

  <h2>Orders ({len(orders_df)})</h2>
  <div class="table-wrap">{_df_to_html_table(orders_df)}</div>

  <h2>Account ({len(account_df)})</h2>
  <div class="table-wrap">{_df_to_html_table(account_df)}</div>
</div>
"""


def print_report(
    engine: BacktestEngine,
    venue,
    title: str,
) -> None:
    print("\n========== BACKTEST COMPLETE ==========")

    stats_pnls = engine.portfolio.analyzer.get_performance_stats_pnls()
    stats_returns = engine.portfolio.analyzer.get_performance_stats_returns()
    stats_general = engine.portfolio.analyzer.get_performance_stats_general()

    print("\n--- Portfolio Performance ---")
    for k, v in {**stats_pnls, **stats_returns, **stats_general}.items():
        print(f"  {k}: {v}")

    positions_report = engine.trader.generate_positions_report()
    print(f"\n--- Positions Report ({len(positions_report)} rows) ---")
    print(positions_report.to_string(max_rows=20))

    fills_report = engine.trader.generate_fills_report()
    print(f"\n--- Fills Report ({len(fills_report)} rows) ---")
    print(fills_report.to_string(max_rows=20))

    orders_report = engine.trader.generate_orders_report()
    print(f"\n--- Orders Report ({len(orders_report)} rows) ---")
    print(orders_report.to_string(max_rows=20))

    account_report = engine.trader.generate_account_report(venue)
    print(f"\n--- Account Report ({len(account_report)} rows) ---")
    print(account_report.to_string(max_rows=10))

    run_id = engine.run_id
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    tearsheet_path = reports_dir / f"tearsheet_{run_id}.html"

    print(f"\n--- Generating tearsheet ({run_id}) ---")
    create_tearsheet(
        engine,
        output_path=str(tearsheet_path),
        title=title,
    )

    html = tearsheet_path.read_text()

    reports_html = _build_reports_html(
        stats_pnls=stats_pnls,
        stats_returns=stats_returns,
        stats_general=stats_general,
        positions_df=positions_report,
        fills_df=fills_report,
        orders_df=orders_report,
        account_df=account_report,
    )

    html = html.replace("</body>", reports_html + "\n</body>")
    tearsheet_path.write_text(html)
    print("Report sections and chart injected into tearsheet.")

    print(f"Tearsheet saved to {tearsheet_path}")

    html_path = tearsheet_path.resolve()
    webbrowser.open(f"file://{html_path}")

    print("\n========== DONE ==========")
