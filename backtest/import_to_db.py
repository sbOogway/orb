#!/usr/bin/env python3
"""Scan the CSV data folder and import everything into SQLite.

Usage:
    uv run python3 import_to_db.py --data-dir ../data
    uv run python3 import_to_db.py --data-dir ../data --db /path/to/custom.db
"""

import argparse
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from db import DB_PATH, scan_and_import


def main():
    parser = argparse.ArgumentParser("Import CSV data folder to SQLite")
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parent.parent / "data"))
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    print(f"Importing {args.data_dir} -> {args.db}")
    totals = scan_and_import(args.data_dir, args.db)
    print(f"Done. {len(totals)} tables, {sum(totals.values()):,} total rows.")
    for tbl, n in sorted(totals.items()):
        print(f"  {tbl}: {n:,} rows")


if __name__ == "__main__":
    main()
