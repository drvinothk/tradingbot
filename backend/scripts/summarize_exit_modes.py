"""Reads the canonical per-exit-mode trade CSVs `run_backtest.py --exit-mode
all` (+ `merge_backtest_shards.py` for a sharded run) produces and prints:

1. An exit-mode comparison table, PnL standardized to 1 lot so every mode
   is directly comparable regardless of its own --total-lots sizing.
   'legacy'/'current'/'target_mult' are single-leg modes whose own `pnl`
   column already reflects that row's own real `qty_lots` (2026-08-27:
   'current' carries a real `qty_lots` -- api.v1.strategies
   ._DEFAULT_QTY_LOTS_PAPER, 10 today -- unlike 'legacy'/'target_mult',
   which are always pinned at 1; see run_backtest.py's `_reconstruct_exit`
   vs `_reconstruct_exit_current`) -- so each entry is standardized by
   dividing by *that row's own* `qty_lots` value, not a mode-wide constant
   (2026-08-27 fix -- a single shared "already 1-lot" assumption broke the
   moment a second single-leg mode with a different real qty_lots existed;
   see `_one_lot_entries`). 'near_only'/'far_only'/'no_target_only'/
   'split_30_30_40' are scaled to --total-lots by run_backtest.py's own
   reconstruction, so dividing by --total-lots remains correct for those.
   `split_30_30_40` is additionally multi-leg (3 rows per entry) -- its
   1-lot PnL is computed per *entry* (sum the entry's leg rows, then
   divide), never by summing each leg's own row-level pnl/qty_lots
   independently, since the legs carry different per-lot outcomes (a near
   leg's own per-lot PnL isn't the split strategy's per-lot PnL).
2. A day-of-week breakdown (trades/win-rate/pnl) per exit-mode.

Read-only, diagnostic -- never modifies the source CSVs.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXIT_MODES = (
    "legacy", "current", "near_only", "far_only", "no_target_only", "split_30_30_40",
    "target_mult",
)


def _load(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="") as f:
        return list(csv.DictReader(f))


# Single-leg modes: one row per entry, and that row's own `qty_lots` column
# is the real size to standardize by (not a mode-wide constant -- 'legacy'/
# 'target_mult' are always pinned qty_lots=1, but 'current' (2026-08-27)
# carries a real qty_lots that can differ, so a single shared divisor across
# this whole set would be wrong the moment they don't match). Multi-leg
# modes (near_only/far_only/no_target_only/split_30_30_40) are scaled to
# `--total-lots` by run_backtest.py's own reconstruction instead -- see
# `_one_lot_entries` below for how the two are told apart per-row.
_SINGLE_LEG_MODES = frozenset({"legacy", "current", "target_mult"})


def _one_lot_entries(
    rows: list[dict[str, str]], mode: str, total_lots: int
) -> list[tuple[str, float]]:
    """Returns (entry_time, one_lot_pnl) per entry -- one row per entry
    regardless of how many legs (rows) that entry had in the source CSV.

    For a single-leg mode (`_SINGLE_LEG_MODES`), each entry is divided by
    *that row's own* `qty_lots` column value (2026-08-27 fix -- self-
    adjusting if `api.v1.strategies._DEFAULT_QTY_LOTS_PAPER` or any other
    single-leg mode's own qty_lots ever changes again, rather than a second
    hardcoded mode-name check to keep in sync by hand). For every other
    mode, divided by the caller-supplied `total_lots` (`--total-lots`),
    since those rows are scaled to that value by run_backtest.py's own
    reconstruction, not by their own qty_lots column.
    """
    by_entry: dict[tuple[str, str], float] = defaultdict(float)
    qty_lots_by_entry: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row["symbol"], row["entry_time"])
        pnl = row["pnl"]
        if pnl != "":
            by_entry[key] += float(pnl)
        if mode in _SINGLE_LEG_MODES:
            qty_lots_by_entry[key] = int(row["qty_lots"])
    results: list[tuple[str, float]] = []
    for key, total in by_entry.items():
        divisor = qty_lots_by_entry[key] if mode in _SINGLE_LEG_MODES else total_lots
        results.append((key[1], total / divisor))
    return results


def _weekday(iso_ts: str) -> str:
    return datetime.fromisoformat(iso_ts).strftime("%A")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--underlying", required=True)
    parser.add_argument("--total-lots", type=int, default=10)
    parser.add_argument(
        "--data-dir", type=Path, default=REPO_ROOT / "data" / "historical" / "backtest_reports"
    )
    args = parser.parse_args()

    print(f"\n{'=' * 90}\nExit-mode comparison -- {args.strategy}/{args.underlying}, "
          f"PnL standardized to 1 lot\n{'=' * 90}")
    print(
        f"{'mode':<18} {'entries':>8} {'win%':>7} {'total_pnl':>12} "
        f"{'avg_win':>9} {'avg_loss':>10} {'profit_factor':>14}"
    )
    for mode in EXIT_MODES:
        path = args.data_dir / f"{args.strategy}_{args.underlying}_trades_{mode}.csv"
        if not path.is_file():
            print(f"{mode:<18} (missing: {path.name})")
            continue
        rows = _load(path)
        entries = _one_lot_entries(rows, mode, args.total_lots)
        n = len(entries)
        wins = [p for _, p in entries if p > 0]
        losses = [p for _, p in entries if p <= 0]
        win_rate = (len(wins) / n * 100) if n else 0.0
        total_pnl = sum(p for _, p in entries)
        avg_win = (sum(wins) / len(wins)) if wins else 0.0
        avg_loss = (sum(losses) / len(losses)) if losses else 0.0
        gross_win, gross_loss = sum(wins), abs(sum(losses))
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
        pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
        print(
            f"{mode:<18} {n:>8} {win_rate:>6.1f}% {total_pnl:>+12.2f} "
            f"{avg_win:>+9.2f} {avg_loss:>+10.2f} {pf_str:>14}"
        )

    print(f"\n{'=' * 90}\nDay-of-week breakdown per exit-mode (1-lot standardized)\n{'=' * 90}")
    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    for mode in EXIT_MODES:
        path = args.data_dir / f"{args.strategy}_{args.underlying}_trades_{mode}.csv"
        if not path.is_file():
            continue
        rows = _load(path)
        entries = _one_lot_entries(rows, mode, args.total_lots)
        by_day: dict[str, list[float]] = defaultdict(list)
        for entry_time, pnl in entries:
            by_day[_weekday(entry_time)].append(pnl)

        print(f"\n-- {mode} --")
        print(f"{'day':<12} {'trades':>7} {'win%':>7} {'total_pnl':>12}")
        for day in weekday_order:
            pnls = by_day.get(day, [])
            if not pnls:
                continue
            wins = [p for p in pnls if p > 0]
            win_rate = len(wins) / len(pnls) * 100
            print(f"{day:<12} {len(pnls):>7} {win_rate:>6.1f}% {sum(pnls):>+12.2f}")


if __name__ == "__main__":
    main()
