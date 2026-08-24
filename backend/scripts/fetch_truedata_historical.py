"""Fetches and saves real historical market data from TrueData for offline
backtesting: long-history EOD futures/index/VIX bars (Layer 1/2 of the
backtest plan), the current near-term option chain at 1-min resolution
(Layer 3), and — new — a past-year archive of expired weekly/monthly
option chains reconstructed contract-by-contract at BOTH EOD and real
1-min resolution (a genuinely useful, not just coarse, TrueData-only
stand-in for Layer 3/4's "longer-history expired-option replay", see the
module docstring's own caveats below).

Standalone: talks to TrueData's historical REST service (`TD_hist`,
`history.truedata.in`) directly, works regardless of market hours, and
never touches `MARKET_DATA_PROVIDER`/the live app/DB — this only writes
CSV files to disk.

Requires `config/credentials/truedata.env` (see `truedata.env.example`)
and the `truedata` package. **Install `truedata` into its own separate
venv, never the shared backend `.venv`** — it pulls in pandas, whose numpy
dependency breaks `mypy app tests` (this project's python_version = "3.11"
target) the moment numpy exists anywhere in the environment (see the
`truedata` extra's own comment in `pyproject.toml`). e.g.:
    python -m venv .venv-truedata
    ./.venv-truedata/Scripts/pip install truedata>=7.0 "pydantic-settings>=2.5" \
        "pydantic[email]>=2.9"
    ./.venv-truedata/Scripts/python scripts/fetch_truedata_historical.py

Usage:
    python scripts/fetch_truedata_historical.py
    python scripts/fetch_truedata_historical.py --duration "15 D" --strikes 10
    python scripts/fetch_truedata_historical.py --skip-past-options   # quick top-up only

Saves CSV files under backend/data/historical/ (gitignored — see
.gitignore's own comment on why this is treated as regenerable output,
not source):
    underlyings/NIFTY_1min.csv, BANKNIFTY_1min.csv, INDIA_VIX_1min.csv
        — near-term 1-min bars, merged with whatever was already saved
          (deduped/sorted by timestamp) so reruns extend coverage forward
          instead of losing days that fall outside TrueData's rolling
          intraday window (see the real limit below).
    underlyings_eod/NIFTY_FUT_eod.csv, BANKNIFTY_FUT_eod.csv (continuous
        futures, `NIFTY-I`/`BANKNIFTY-I`), NIFTY_INDEX_eod.csv,
        BANKNIFTY_INDEX_eod.csv (spot, for reference), INDIA_VIX_eod.csv
        — the long-history daily benchmark series for Layer 1/2.
    options/<underlying>/<expiry-YYYY-MM-DD>/<symbol>.csv
        — current near-term option chain, 1-min bars, merged same as the
          underlying 1-min files above.
    options_eod/<underlying>/<expiry-YYYY-MM-DD>/<symbol>.csv
        — past-year archive of now-expired option contracts, EOD bars.
    options_1min_past/<underlying>/<expiry-YYYY-MM-DD>/<symbol>.csv
        — the SAME past-year archive at real 1-min resolution (see Layer
          3/4 caveat below for why this needs a contract-scoped date
          window rather than a blanket multi-year pull).

Real per-row columns (confirmed live 2026-08-17, see
`truedata_provider.py`'s own module docstring): timestamp,open,high,low,
close,volume,oi. `oi`/`volume` are always 0 for underlyings/VIX (real —
an index has no traded volume/OI of its own; the continuous futures series
`NIFTY-I`/`BANKNIFTY-I` does carry real, non-zero volume/OI, confirmed
live), and real, non-zero for option contracts.

**Real, live-confirmed limits (2026-08-23, this trial account — corrects
two of this docstring's own prior claims, neither independently
re-verified before now):**
    - **The ~11-12 calendar day intraday cap is real, but only for symbols
      that are still currently active/live** — `NIFTY-I`/`BANKNIFTY-I`
      (the continuous, rolling futures symbols), the plain index/spot
      symbols, and any option contract that hasn't expired yet. Confirmed
      three independent ways: requesting 30D/60D `duration` returns the
      same ~11-12 day window; requesting an *explicit* 45/60-day
      `start_time` on the still-listed near-term option contract gets the
      same cap (ruling out `duration`'s own today-relative computation as
      the cause); the cap applies at every bar size tested (1/2/3/5/10/
      15/30/60 min), not just 1-min.
    - **Once a contract has genuinely expired, real intraday data (1-min
      and every coarser size tested) is available for its entire past
      listed life — a full year back, not just EOD.** This directly
      corrects this script's own prior conclusion (from an earlier
      session) that only EOD survives past ~12 days — that conclusion was
      an artifact of testing via `duration` (relative to *today*), which
      does NOT work for old dates: `get_historic_data(symbol,
      duration="3 Y", bar_size="1 min")` on a real, populated, long-expired
      contract returns EMPTY (almost certainly a server-side guard against
      a multi-year request at 1-min resolution), while the exact same
      symbol with an explicit `start_time`/`end_time` window scoped to its
      own real trading life (~40-80 days around its expiry) returns full
      data at every granularity down to 1-min. Confirmed on a NIFTY
      contract that expired 2025-09-02 (a full year old) and one that
      expired 2026-05-05.
    - EOD (`bar_size="eod"`, NOT "1 day" — that string silently returns
      empty; TrueData's own REST client only recognizes the literal
      string `"eod"`) bars for continuous futures/index/VIX: a real ~3
      years is available (confirmed: requesting "5 Y" returns the exact
      same 743 rows, 2023-08-24 onward, as "3 Y" does — the account's real
      ceiling is ~3 years, not 5, despite the duration string accepting a
      "Y" unit).
    - Both EOD and 1-min for individual **option contracts** are bounded to
      roughly the past year and to each contract's own listed lifetime
      (typically ~1 month of real trading days before NIFTY's weekly
      expiry, ~2 months for BANKNIFTY's monthly expiry — not a multi-year
      archive per symbol): a NIFTY contract expiring 2025-08-19 (and
      BANKNIFTY expiring 2025-08-26) returned nothing at all across a full
      strike grid, both option types — real, not a strike-selection miss.

This means the user-supplied backtest plan's Layer 1 ("2-5 years of NIFTY
Futures data") is achievable at EOD granularity via `NIFTY-I`/
`BANKNIFTY-I`, and Layer 3/4's "longer-history expired-option replay" is
**genuinely, not just coarsely, coverable by this same TrueData account
for roughly the past year** — full 1-min contract-level bars, not only
EOD — via `--past-days` below. Still short of a true multi-year *intraday*
option archive, and this account's own live/near-term data (the currently
still-listed chain) really is capped at ~11-12 days intraday regardless —
so a separate vendor is still the answer for anything older than ~1 year
at intraday resolution, per the build plan's own Layer 3/4 distinction.

**Past-expiry reconstruction is a real approximation, not exact**: past
NIFTY weekly / BANKNIFTY monthly expiry Tuesdays are computed by plain
calendar arithmetic (every Tuesday for NIFTY; last Tuesday of the month for
BANKNIFTY) — this does NOT account for NSE holidays that shift a real
expiry off Tuesday (e.g. onto the preceding Monday). Weeks where the guess
is wrong will just come back empty for every strike (tolerated, logged, not
fatal) — check the per-expiry "0/N contracts had real data" lines in the
output before trusting a given week's folder is complete.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import get_settings  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "historical"

# (internal underlying symbol, TrueData's real index symbol, TrueData's
# real continuous-futures symbol confirmed from the installed SDK's own
# `truedata/tests/defaults.py` (`DEFAULT_TEST_SYMBOL_LIST`), current
# near-term expiry, strike step) — expiries confirmed live/current as of
# 2026-08-23 (both NIFTY's weekly and BANKNIFTY's monthly near-term
# contract land on the same date this particular week — coincidence, not
# a rule). Update these when they roll over.
UNDERLYINGS: list[tuple[str, str, str, date, int]] = [
    ("NIFTY", "NIFTY 50", "NIFTY-I", date(2026, 8, 25), 50),
    ("BANKNIFTY", "NIFTY BANK", "BANKNIFTY-I", date(2026, 8, 25), 100),
]


def _option_symbol(underlying: str, expiry: date, strike: int, option_type: str) -> str:
    """TrueData's real option-symbol convention (confirmed live 2026-08-17
    from the installed SDK's own TD_chain.py): plain underlying name, no
    space, expiry as %y%m%d, then the strike, then a CE/PE suffix — a
    different convention from Shoonya's own P/C-suffix format, so don't
    reuse any Shoonya symbol-construction code here.
    """
    return f"{underlying}{expiry.strftime('%y%m%d')}{strike}{option_type}"


def _merge_and_save_df(df: object, path: Path) -> tuple[int, int]:
    """Merge newly-fetched rows with whatever is already on disk at
    `path`, deduped and sorted by timestamp, so a rerun extends coverage
    forward instead of clobbering days that fall outside TrueData's
    rolling window. Returns (total_rows_saved, new_rows_added).
    """
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    new_df = df if (df is not None and not df.empty) else None  # type: ignore[union-attr]
    if new_df is not None:
        # Existing CSVs round-trip through pd.read_csv as plain strings (no
        # parse_dates); a freshly-fetched frame's timestamp column is a real
        # pandas Timestamp dtype. Mixing the two in one column makes both
        # concat and sort_values crash (`'<' not supported between instances
        # of 'Timestamp' and 'str'`) — normalize to the same string form
        # pd.read_csv would produce before ever concatenating.
        new_df = new_df.copy()
        new_df["timestamp"] = new_df["timestamp"].astype(str)

    if path.exists():
        existing = pd.read_csv(path)
        existing_count = len(existing)
        combined = (
            pd.concat([existing, new_df], ignore_index=True) if new_df is not None else existing
        )
    else:
        existing_count = 0
        if new_df is None:
            return 0, 0
        combined = new_df

    combined = combined.drop_duplicates(subset=["timestamp"], keep="last")
    combined = combined.sort_values("timestamp").reset_index(drop=True)
    combined.to_csv(path, index=False)
    total = len(combined)
    return total, total - existing_count


def _past_weekly_expiries(before: date, days_back: int) -> list[date]:
    """Every past Tuesday in [before - days_back, before) — NIFTY's real
    weekly expiry day. See module docstring's holiday caveat.
    """
    start = before - timedelta(days=days_back)
    expiries = []
    d = start
    while d.weekday() != 1:
        d += timedelta(days=1)
    while d < before:
        expiries.append(d)
        d += timedelta(days=7)
    return expiries


def _past_monthly_expiries(before: date, days_back: int) -> list[date]:
    """Last Tuesday of each past month in [before - days_back, before) —
    BANKNIFTY's real monthly expiry day (NSE's post-2024
    single-weekly-index-per-exchange rule). See module docstring's
    holiday caveat.
    """
    import calendar

    start = before - timedelta(days=days_back)
    expiries = []
    year, month = start.year, start.month
    while True:
        last_day = calendar.monthrange(year, month)[1]
        d = date(year, month, last_day)
        while d.weekday() != 1:
            d -= timedelta(days=1)
        if start <= d < before:
            expiries.append(d)
        if year == before.year and month == before.month:
            break
        month += 1
        if month > 12:
            month = 1
            year += 1
    return expiries


def _atm_from_eod(eod_df: object, on_or_before: date, strike_step: int) -> float | None:
    import pandas as pd

    if eod_df is None or eod_df.empty:  # type: ignore[union-attr]
        return None
    df = eod_df.copy()  # type: ignore[union-attr]
    df["_d"] = pd.to_datetime(df["timestamp"]).dt.date
    candidates = df[df["_d"] <= on_or_before]
    if candidates.empty:
        return None
    spot = float(candidates.iloc[-1]["close"])
    return round(spot / strike_step) * strike_step


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--duration", default="15 D", help="TD_hist duration string for near-term 1-min bars"
    )
    parser.add_argument(
        "--strikes",
        type=int,
        default=10,
        help="Strikes on each side of ATM for the near-term chain (default 10 -> 21x2=42)",
    )
    parser.add_argument(
        "--eod-duration",
        default="3 Y",
        help="TD_hist duration string for long-history EOD bars (real cap ~3 Y)",
    )
    parser.add_argument(
        "--eod-strikes",
        type=int,
        default=5,
        help="Strikes on each side of ATM per past expiry (default 5 -> 11x2=22)",
    )
    parser.add_argument(
        "--past-days",
        type=int,
        default=365,
        help="How many days back to reconstruct expired option-chain EOD bars",
    )
    parser.add_argument(
        "--skip-past-options",
        action="store_true",
        help="Skip the (slow, many-request) past-expiry option EOD reconstruction",
    )
    args = parser.parse_args()

    from truedata.history.TD_hist import TD_hist

    settings = get_settings().truedata
    if not settings.username or not settings.password.get_secret_value():
        raise SystemExit(
            "TrueData credentials missing — fill in "
            "app/config/credentials/truedata.env (see truedata.env.example next to it)."
        )

    print(f"Connecting to TrueData historical service as {settings.username}...")
    td_hist = TD_hist(settings.username, settings.password.get_secret_value())
    print("Connected.\n")

    # --- Layer 1/2: long-history EOD benchmark series ---
    print("=== Long-history EOD bars (Layer 1/2 benchmark) ===")
    eod_futures_by_underlying: dict[str, object] = {}
    vix_df = td_hist.get_historic_data("INDIA VIX", duration=args.eod_duration, bar_size="eod")
    n, added = _merge_and_save_df(vix_df, DATA_DIR / "underlyings_eod" / "INDIA_VIX_eod.csv")
    print(f"INDIA VIX eod: {n} rows total ({added} new)")

    for underlying, td_index_symbol, td_fut_symbol, _expiry, _strike_step in UNDERLYINGS:
        fut_df = td_hist.get_historic_data(
            td_fut_symbol, duration=args.eod_duration, bar_size="eod"
        )
        n, added = _merge_and_save_df(
            fut_df, DATA_DIR / "underlyings_eod" / f"{underlying}_FUT_eod.csv"
        )
        print(f"{underlying} ({td_fut_symbol}) futures eod: {n} rows total ({added} new)")
        eod_futures_by_underlying[underlying] = fut_df

        idx_df = td_hist.get_historic_data(
            td_index_symbol, duration=args.eod_duration, bar_size="eod"
        )
        n, added = _merge_and_save_df(
            idx_df, DATA_DIR / "underlyings_eod" / f"{underlying}_INDEX_eod.csv"
        )
        print(f"{underlying} ({td_index_symbol}) index eod: {n} rows total ({added} new)")
    print()

    # --- Layer 3: near-term option chain, 1-min ---
    print("=== Near-term option chain (Layer 3, 1-min bars) ===")
    for underlying, td_index_symbol, _td_fut_symbol, expiry, strike_step in UNDERLYINGS:
        df = td_hist.get_historic_data(td_index_symbol, duration=args.duration, bar_size="1 min")
        n, added = _merge_and_save_df(df, DATA_DIR / "underlyings" / f"{underlying}_1min.csv")
        print(f"{underlying}: {n} underlying 1-min rows total ({added} new)")
        if df is None or df.empty:  # type: ignore[union-attr]
            print(f"  skipping options for {underlying} — no underlying data to derive ATM from")
            continue

        spot = float(df.iloc[-1]["close"])
        atm = round(spot / strike_step) * strike_step
        strikes = [atm + i * strike_step for i in range(-args.strikes, args.strikes + 1)]
        print(
            f"  spot={spot:.2f} atm={atm} fetching {len(strikes) * 2} option contracts "
            f"(expiry {expiry.isoformat()})..."
        )

        fetched = 0
        for strike in strikes:
            for option_type in ("CE", "PE"):
                symbol = _option_symbol(underlying, expiry, strike, option_type)
                try:
                    opt_df = td_hist.get_historic_data(
                        symbol, duration=args.duration, bar_size="1 min"
                    )
                except Exception as exc:  # noqa: BLE001 - one bad contract must not abort the run
                    print(f"  {symbol}: FAILED ({exc!r})")
                    continue
                n, added = _merge_and_save_df(
                    opt_df,
                    DATA_DIR / "options" / underlying / expiry.isoformat() / f"{symbol}.csv",
                )
                if n:
                    fetched += 1
                    print(f"  {symbol}: {n} rows total ({added} new)")
                else:
                    print(f"  {symbol}: no data (not listed / no trades in window)")
        print(f"  {underlying}: {fetched}/{len(strikes) * 2} option contracts had real data\n")

    # --- Layer 3/4 partial: past-year expired option-chain archive ---
    # Real 1-min (and finer) intraday data DOES exist for expired contracts across
    # the full past year — confirmed live 2026-08-23, correcting this script's own
    # earlier assumption that only EOD survives past ~12 days. The catch: it only
    # comes back when queried with an explicit, narrow, contract-scoped start/end
    # window (`duration="3 Y"` at 1-min returns EMPTY even for a real, populated
    # contract — almost certainly a server-side size guard against a
    # multi-year-at-1-min request). So each past contract gets its own
    # `start_time`/`end_time` window sized to roughly its real listed life, not a
    # blanket multi-year pull.
    if args.skip_past_options:
        print("Skipping past-expiry option reconstruction (--skip-past-options).")
    else:
        from datetime import datetime as _datetime

        print(
            f"=== Past-expiry option chains, EOD + 1-min bars, last {args.past_days} days "
            "(partial Layer 3/4 stand-in — see module docstring caveats) ==="
        )
        for underlying, _td_index_symbol, _td_fut_symbol, expiry, strike_step in UNDERLYINGS:
            fut_df = eod_futures_by_underlying.get(underlying)
            is_monthly = underlying == "BANKNIFTY"
            past_expiries = (
                _past_monthly_expiries(expiry, args.past_days)
                if is_monthly
                else _past_weekly_expiries(expiry, args.past_days)
            )
            # Real observed listed-life spans (2026-08-23): NIFTY weekly ~36 days,
            # BANKNIFTY monthly ~64 days before its own expiry — padded for safety.
            lookback_days = 80 if is_monthly else 45
            print(f"{underlying}: {len(past_expiries)} past expiry dates to attempt")
            for past_expiry in past_expiries:
                atm = _atm_from_eod(fut_df, past_expiry, strike_step)
                if atm is None:
                    print(f"  {past_expiry.isoformat()}: no EOD spot available, skipping")
                    continue
                strikes = [
                    int(atm + i * strike_step)
                    for i in range(-args.eod_strikes, args.eod_strikes + 1)
                ]
                window_start = _datetime.combine(
                    past_expiry - timedelta(days=lookback_days), _datetime.min.time()
                )
                window_end = _datetime.combine(past_expiry, _datetime.min.time()).replace(
                    hour=23, minute=59, second=59
                )
                fetched_eod = 0
                fetched_1min = 0
                for strike in strikes:
                    for option_type in ("CE", "PE"):
                        symbol = _option_symbol(underlying, past_expiry, strike, option_type)
                        try:
                            eod_df = td_hist.get_historic_data(
                                symbol,
                                start_time=window_start,
                                end_time=window_end,
                                bar_size="eod",
                            )
                        except Exception:  # noqa: BLE001
                            eod_df = None
                        n, _ = _merge_and_save_df(
                            eod_df,
                            DATA_DIR
                            / "options_eod"
                            / underlying
                            / past_expiry.isoformat()
                            / f"{symbol}.csv",
                        )
                        if n:
                            fetched_eod += 1
                        time.sleep(0.05)

                        try:
                            min_df = td_hist.get_historic_data(
                                symbol,
                                start_time=window_start,
                                end_time=window_end,
                                bar_size="1 min",
                            )
                        except Exception:  # noqa: BLE001
                            min_df = None
                        n, _ = _merge_and_save_df(
                            min_df,
                            DATA_DIR
                            / "options_1min_past"
                            / underlying
                            / past_expiry.isoformat()
                            / f"{symbol}.csv",
                        )
                        if n:
                            fetched_1min += 1
                        time.sleep(0.05)
                print(
                    f"  {past_expiry.isoformat()} (atm~{atm:.0f}): "
                    f"eod {fetched_eod}/{len(strikes) * 2}, "
                    f"1min {fetched_1min}/{len(strikes) * 2} contracts had real data"
                )
        print()

    print(f"Done. Data saved under {DATA_DIR}")


if __name__ == "__main__":
    main()
