"""Thin wrapper that imports the canonical db module from the project root.

Avoids circular imports by loading via importlib.
"""

import importlib.util
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("_root_db", _root / "db.py")
_root_db = importlib.util.module_from_spec(_spec)
sys.modules["_root_db"] = _root_db
_spec.loader.exec_module(_root_db)

# Re-export everything
DB_PATH = _root_db.DB_PATH
SOURCE_NAMES = _root_db.SOURCE_NAMES
get_connection = _root_db.get_connection
table_name = _root_db.table_name
ensure_candle_table = _root_db.ensure_candle_table
import_csv = _root_db.import_csv
scan_and_import = _root_db.scan_and_import
load_ticker_df = _root_db.load_ticker_df
ensure_cache_table = _root_db.ensure_cache_table
is_day_cached = _root_db.is_day_cached
mark_day_cached = _root_db.mark_day_cached
clear_day_cache = _root_db.clear_day_cache
clear_ticker_cache = _root_db.clear_ticker_cache
count_cached = _root_db.count_cached
ensure_tickers_table = _root_db.ensure_tickers_table
upsert_tickers = _root_db.upsert_tickers
get_tickers = _root_db.get_tickers
