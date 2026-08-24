"""Fetches a year of real 1-min NIFTY/BANKNIFTY *futures* data, stitched
into a continuous underlying-proxy series, to stand in for the spot
index's own 1-min history where that's unavailable.

**Why this exists**: `underlyings/{underlying}_1min.csv` (the plain spot
index, fetched by `fetch_truedata_historical.py`) is capped at ~11-12
calendar days intraday — confirmed live, see that script's own docstring
— because the spot/index symbol is always "still active/live" on
TrueData's account and never crosses into the "genuinely expired, full
year of 1-min available" bucket that option contracts do. A specific
**dated monthly futures contract** (e.g. `NIFTY25AUGFUT`, as opposed to
the continuous roll symbol `NIFTY-I`) DOES genuinely expire each month —
live-probed 2026-08-23 (see project memory / CLAUDE.md): `NIFTY25AUGFUT`
with an explicit start/end window returned 3,381 real 1-min rows spanning
its whole real trading life (2025-08-13 -> 2025-08-28), the same
"once expired, full intraday history" behavior already proven for options.
This script fetches one such contract per calendar month across the
window needed to backtest the existing `options_1min_past/` archive
(currently Aug 2025 - Aug 2026), and concatenates them into one
continuous series per underlying.

**Real symbol convention** (live-confirmed, not guessed): `{underlying}
{YY}{MMM}FUT` — e.g. `NIFTY25AUGFUT`, `BANKNIFTY25AUGFUT`. Several other
plausible formats (`NIFTY-AUG2025-FUT`, `NIFTY25AUG25FUT`, etc.) were
tried and returned zero rows; only this exact format works.

**Futures price is not spot price** (a real, deliberate approximation,
same "flag it, don't pretend it's exact" discipline this codebase uses
everywhere): futures trade at a basis (carry cost) over spot, typically a
handful of points for NIFTY/BANKNIFTY, widening slightly into expiry.
Intraday *moves* (the thing EMA9/EMA20/pullback-structure strategies
actually key off) track spot extremely closely; the *absolute level* will
be a little off, which matters for strike-selection distance-from-ATM
math but not for the relative up/down structure a technical strategy
reads. Flagged here for whoever tunes ATM-selection against this data.

**No rollover logic beyond raw concatenation**: each month's dated
contract is used for its own entire real trading window as returned by
TrueData (no attempt to trim to "front month only" near expiry, when the
next month's contract may already be more liquid) — on a timestamp
collision between two adjacent months' files (both trading the same
day near rollover), the later month's row wins, since the front-month
role is shifting to it by then. This is a simplification, not a precise
volume-weighted continuous-futures construction (which is what
`NIFTY-I` itself is meant to be, were it not date-capped).

Usage:
    ./.venv-truedata/Scripts/python scripts/fetch_truedata_futures_underlying_history.py

Requires the same `config/credentials/truedata.env` +
`.venv-truedata` setup as `fetch_truedata_historical.py` (see that
script's own docstring) — deliberately not re-explained here.

Output:
    data/historical/underlyings_futures_1min_past/<underlying>/<YYYY-MM>.csv
        — one raw file per contract-month, exactly as returned.
    data/historical/underlyings/<underlying>_FUT_1min.csv
        — all months concatenated, deduped/sorted by timestamp: the
          continuous underlying-proxy series backtests should read
          instead of the too-short spot `<underlying>_1min.csv`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import get_settings  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "historical"

UNDERLYINGS = ("NIFTY", "BANKNIFTY")

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]


def _month_range(start: date, end: date) -> list[date]:
    """First-of-month dates from `start`'s month through `end`'s month, inclusive."""
    months: list[date] = []
    cur = date(start.year, start.month, 1)
    stop = date(end.year, end.month, 1)
    while cur <= stop:
        months.append(cur)
        year = cur.year + (1 if cur.month == 12 else 0)
        month = 1 if cur.month == 12 else cur.month + 1
        cur = date(year, month, 1)
    return months


def _futures_symbol(underlying: str, month_start: date) -> str:
    return f"{underlying}{month_start.strftime('%y%b').upper()}FUT"


def _fetch_month(td_hist: object, symbol: str, month_start: date) -> object:
    """Explicit start/end window scoped generously around the contract's
    real trading life (a few days before month-start through a few days
    into the next month, to catch early listing / any late expiry-day
    settlement quirk) — the same "explicit window, not duration" approach
    already proven necessary for old dates in fetch_truedata_historical.py.
    """
    next_month_year = month_start.year + (1 if month_start.month == 12 else 0)
    next_month = 1 if month_start.month == 12 else month_start.month + 1
    window_start = datetime.combine(month_start - timedelta(days=5), datetime.min.time())
    window_end = datetime.combine(
        date(next_month_year, next_month, 1) + timedelta(days=5), datetime.min.time()
    )
    return td_hist.get_historic_data(  # type: ignore[attr-defined]
        symbol, start_time=window_start, end_time=window_end, bar_size="1 min"
    )


def _save_month(df: object, path: Path) -> int:
    import pandas as pd  # noqa: PLC0415

    if df is None or len(df) == 0:  # type: ignore[arg-type]
        return 0
    frame = df[COLUMNS].copy()  # type: ignore[index]
    frame["timestamp"] = pd.to_datetime(frame["timestamp"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    return len(frame)


def _stitch_continuous(underlying: str, month_dir: Path, out_path: Path) -> int:
    import pandas as pd  # noqa: PLC0415

    frames = []
    for csv_path in sorted(month_dir.glob("*.csv")):
        frames.append(pd.read_csv(csv_path, parse_dates=["timestamp"]))
    if not frames:
        return 0
    combined = pd.concat(frames, ignore_index=True)
    # Later month's row wins on a same-timestamp collision (rollover overlap) --
    # concat is already in chronological month order, so keep="last".
    combined = combined.sort_values("timestamp").drop_duplicates(subset="timestamp", keep="last")
    combined["timestamp"] = combined["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(out_path, index=False)
    return len(combined)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from", dest="from_date", type=date.fromisoformat, default=date(2025, 8, 1)
    )
    parser.add_argument("--to", dest="to_date", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()

    from truedata.history.TD_hist import TD_hist  # noqa: PLC0415

    settings = get_settings().truedata
    if not settings.username or not settings.password.get_secret_value():
        raise SystemExit("TrueData credentials not configured (config/credentials/truedata.env)")
    td_hist = TD_hist(settings.username, settings.password.get_secret_value())

    months = _month_range(args.from_date, args.to_date)
    print(f"Fetching {len(months)} contract-months for {UNDERLYINGS}: "
          f"{months[0].isoformat()} .. {months[-1].isoformat()}")

    for underlying in UNDERLYINGS:
        month_dir = DATA_DIR / "underlyings_futures_1min_past" / underlying
        for month_start in months:
            symbol = _futures_symbol(underlying, month_start)
            out_file = month_dir / f"{month_start.isoformat()[:7]}.csv"
            df = _fetch_month(td_hist, symbol, month_start)
            n = _save_month(df, out_file)
            print(f"  {symbol}: {n} rows" + ("" if n else "  <-- EMPTY, check symbol/expiry"))

        continuous_out = DATA_DIR / "underlyings" / f"{underlying}_FUT_1min.csv"
        total = _stitch_continuous(underlying, month_dir, continuous_out)
        print(f"{underlying}: stitched continuous series -> {continuous_out} ({total} rows)")


if __name__ == "__main__":
    main()
