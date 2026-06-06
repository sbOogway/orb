#!/usr/bin/env bash
# Fetch 5 years of 5-min IBKR data for major stocks
# Usage: ./fetch_major_stocks.sh [--dry-run]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$ROOT_DIR/data"
PORT=7496
YEARS=5

TICKERS=(
    
    
    # Finance
    JPM BAC GS V MA WFC
    # Consumer
    TSLA WMT KO DIS PG HD MCD NKE
    # Healthcare
    JNJ UNH PFE ABBV MRK
    # Energy & Industrial
    XOM CVX BA CAT GE
    # Other
    BRK-B SPY
    # Tech
    AAPL MSFT GOOGL AMZN NVDA META AMD INTC IBM CSCO ORCL CRM ADBE
)

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

echo "=== Fetching $YEARS years of 5-min IBKR data for ${#TICKERS[@]} tickers ==="
echo "  Data dir:  $DATA_DIR"
echo "  IBKR port: $PORT"
echo "  Dry run:   $DRY_RUN"
echo

for ticker in "${TICKERS[@]}"; do
    if $DRY_RUN; then
        echo "[DRY RUN] uv run fetch-candles --ticker $ticker --years $YEARS --port $PORT --data-dir $DATA_DIR"
    else
        echo "--- Fetching $ticker ---"
        # Cache-aware: skips chunks that already have local data
        # Use --force to re-download everything for a ticker
        uv run fetch-candles --ticker "$ticker" --years "$YEARS" --port "$PORT" --data-dir "$DATA_DIR" -v
        echo "--- Done $ticker ---"
        echo
    fi
done

if $DRY_RUN; then
    echo
    echo "Dry run complete. Run without --dry-run to execute."
fi
