"""Merges N per-shard trade CSVs (from `run_backtest.py --shard-count/
--shard-index`, one process per shard, each with its own `--db-suffix`/
`--out-csv`) into the single canonical CSV a non-sharded run would have
produced -- one header row, every shard's data rows, sorted by entry_time
so the file reads chronologically despite shards processing expiries
round-robin (interleaved), not in date order.

Only ever reads already-completed shard files (this script is meant to run
*after* every shard process has exited normally -- see run_backtest.py's
own `--shard-count` docstring) -- never touches a file mid-write, so there
is no torn-row risk as long as it isn't pointed at a still-running shard's
output.

Usage (one call per exit-mode -- run_backtest.py's own `_out_csv_for`
convention is `<base>_<mode>.csv`, and each shard additionally has its own
`_shard<N>` in the base, so a full `--exit-mode all` sharded sweep needs
one merge call per mode):

    python scripts/merge_backtest_shards.py \\
        --shard-csvs data/historical/backtest_reports/orb_NIFTY_trades_shard0_legacy.csv \\
                     data/historical/backtest_reports/orb_NIFTY_trades_shard1_legacy.csv \\
                     ... \\
        --out data/historical/backtest_reports/orb_NIFTY_trades_legacy.csv

Or, more conveniently, via a glob (shell-expanded or passed as one pattern
with --glob):

    python scripts/merge_backtest_shards.py \\
        --glob "data/historical/backtest_reports/orb_NIFTY_trades_shard*_legacy.csv" \\
        --out data/historical/backtest_reports/orb_NIFTY_trades_legacy.csv
"""

from __future__ import annotations

import argparse
import csv
import glob as glob_module
from pathlib import Path

# Kept as an explicit local copy, not an import from run_backtest.py --
# importing that module runs its full app-config/DB-registration import
# chain (see its own module docstring's sys.path.insert dance), which this
# tiny merge-only script has no other reason to pay the cost of.
_TRADE_CSV_HEADER = [
    "symbol", "side", "leg", "entry_time", "entry_price", "exit_time", "exit_price",
    "exit_reason", "qty_lots", "lot_size", "pnl",
    "vix_entry", "vix_exit", "atr_entry", "atr_exit",
    "pcr_entry", "pcr_exit", "contract_oi_entry", "contract_oi_exit",
]


def merge_shard_csvs(shard_paths: list[Path], out_path: Path) -> int:
    """Returns the number of rows written. Raises if any shard file is
    missing, empty, or has an unexpected header -- a silent partial merge
    (e.g. one shard still mid-write, or a genuinely failed shard) would be
    worse than a loud failure here.
    """
    rows: list[dict[str, str]] = []
    for path in shard_paths:
        if not path.is_file():
            raise FileNotFoundError(f"shard CSV not found: {path}")
        with path.open("r", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None or list(reader.fieldnames) != _TRADE_CSV_HEADER:
                raise ValueError(
                    f"{path} header {reader.fieldnames!r} doesn't match expected "
                    f"{_TRADE_CSV_HEADER!r} -- refusing to merge, likely a stale/"
                    "incompatible file from before a CSV-schema change."
                )
            rows.extend(reader)

    # entry_time is written as an ISO-8601 string (to_ist(...).isoformat()
    # in run_backtest.py) -- lexicographic sort on that string is already
    # chronological, no datetime parsing needed.
    rows.sort(key=lambda r: (r["entry_time"], r["symbol"], r["leg"]))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_TRADE_CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    # Atomic on both POSIX and Windows (os.replace, which Path.replace
    # wraps) -- the canonical output file is never observable in a
    # half-written state, unlike writing `out_path` directly.
    tmp_path.replace(out_path)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--shard-csvs", nargs="+", type=Path, help="Explicit list of shard CSV paths."
    )
    group.add_argument(
        "--glob", help="Glob pattern matching shard CSVs (quote it to avoid shell expansion)."
    )
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    shard_paths = (
        args.shard_csvs
        if args.shard_csvs is not None
        else sorted(Path(p) for p in glob_module.glob(args.glob))
    )
    if not shard_paths:
        raise SystemExit(f"No shard files matched (glob={args.glob!r})")

    count = merge_shard_csvs(shard_paths, args.out)
    print(f"Merged {len(shard_paths)} shard file(s), {count} row(s) total, -> {args.out}")


if __name__ == "__main__":
    main()
