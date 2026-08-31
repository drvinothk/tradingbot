"""Refreshes the `vix_entry`/`vix_exit` diagnostic columns on the 5
canonical backtest trade CSVs using the new Alice Blue VIX source
(`underlyings/INDIA_VIX_alice_index_1min.csv`, real 1-min back ~2.6 years —
see `fetch_alice_blue_underlying_history.py`), replacing whatever
TrueData-sourced value (1-min where available, else that day's EOD close)
was recorded when these backtests originally ran.

Only touches `vix_entry`/`vix_exit` — trades/entries/exits/pnl are
untouched, since VIX is diagnostic-only and never fed into any strategy's
own entry/exit condition (see `run_backtest.py`'s `ReconstructedTrade`
docstring). Safe to rerun `label_supporting_param_ranges.py` afterward to
refresh the range-band columns and summary off these corrected values.

Run: `python scripts/refresh_vix_diagnostics.py`
"""

from __future__ import annotations

import csv
from bisect import bisect_right
from datetime import datetime
from pathlib import Path

REPORTS_DIR = Path(__file__).resolve().parent.parent / "data" / "historical" / "backtest_reports"
VIX_PATH = (
    Path(__file__).resolve().parent.parent
    / "data" / "historical" / "underlyings" / "INDIA_VIX_alice_index_1min.csv"
)

TRADE_FILES = [
    "orb_NIFTY_trades.csv",
    "vwap_pullback_NIFTY_trades.csv",
    "ema_micro_pullback_NIFTY_trades.csv",
    "oi_volume_confirmed_NIFTY_trades.csv",
    "liquidity_sweep_reversal_NIFTY_trades.csv",
]


def _load_vix_series() -> list[tuple[datetime, float]]:
    series: list[tuple[datetime, float]] = []
    with VIX_PATH.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
            series.append((ts, float(row["close"])))
    series.sort(key=lambda pair: pair[0])
    return series


def _vix_at(series: list[tuple[datetime, float]], ts: datetime) -> float | None:
    idx = bisect_right([s[0] for s in series], ts) - 1
    return series[idx][1] if idx >= 0 else None


def _parse_trade_ts(raw: str) -> datetime:
    # e.g. "2025-08-19T09:44:00+05:30" -- strip tz, the VIX series is naive IST
    # (same convention every other bar/underlying file in this codebase uses).
    dt = datetime.fromisoformat(raw)
    return dt.replace(tzinfo=None)


def main() -> None:
    if not VIX_PATH.is_file():
        raise SystemExit(f"Missing Alice VIX file: {VIX_PATH}")
    vix_series = _load_vix_series()
    print(f"Loaded {len(vix_series)} VIX rows from {VIX_PATH.name}")

    for fname in TRADE_FILES:
        path = REPORTS_DIR / fname
        if not path.exists():
            print(f"  (skip, not found) {fname}")
            continue
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            fieldnames = list(rows[0].keys()) if rows else []

        changed = 0
        for row in rows:
            old_entry = row.get("vix_entry", "")
            old_exit = row.get("vix_exit", "")
            entry_ts = _parse_trade_ts(row["entry_time"])
            new_entry = _vix_at(vix_series, entry_ts)
            row["vix_entry"] = f"{new_entry}" if new_entry is not None else ""
            if row.get("exit_time"):
                exit_ts = _parse_trade_ts(row["exit_time"])
                new_exit = _vix_at(vix_series, exit_ts)
                row["vix_exit"] = f"{new_exit}" if new_exit is not None else ""
            if row["vix_entry"] != old_entry or row["vix_exit"] != old_exit:
                changed += 1

        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"  refreshed -> {fname} ({len(rows)} trades, {changed} vix values changed)")


if __name__ == "__main__":
    main()
