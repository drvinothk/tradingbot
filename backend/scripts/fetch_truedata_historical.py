"""Fetches and saves real historical market data from TrueData for offline
backtesting — underlying 1-min bars, INDIA VIX, and a band of near-ATM
option-contract 1-min bars (with real per-bar OI) for the current
near-term expiry of NIFTY and BANKNIFTY.

Standalone: talks to TrueData's historical REST service (`TD_hist`,
`history.truedata.in`) directly, works regardless of market hours, and
never touches `MARKET_DATA_PROVIDER`/the live app/DB — this only writes
CSV files to disk.

Requires `config/credentials/truedata.env` (see `truedata.env.example`)
and the `truedata` package (`pip install truedata`).

Usage:
    python scripts/fetch_truedata_historical.py
    python scripts/fetch_truedata_historical.py --duration "15 D" --strikes 10

Saves CSV files under backend/data/historical/ (gitignored — see
.gitignore's own comment on why this is treated as regenerable output,
not source):
    underlyings/NIFTY_1min.csv, BANKNIFTY_1min.csv, INDIA_VIX_1min.csv
    options/<underlying>/<expiry-YYYY-MM-DD>/<symbol>.csv

Real per-row columns (confirmed live 2026-08-17, see
`truedata_provider.py`'s own module docstring): timestamp,open,high,low,
close,volume,oi. `oi`/`volume` are always 0 for underlyings/VIX (real —
an index has no traded volume/OI of its own), and real, non-zero for
option contracts.

Known real limit (TrueData trial account): 1-min bar history is only
reliably available for roughly the last 15 days — `--duration` beyond
that will just return whatever the trial actually has, not an error.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import get_settings  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "historical"

# (internal underlying symbol, TrueData's real symbol, current near-term
# expiry, strike step) — expiries confirmed live/current as of 2026-08-17
# (see CLAUDE.md's Shoonya scrip-master section); NIFTY is weekly
# (Tuesday), BANKNIFTY is monthly (last Tuesday) per NSE's post-2024
# single-weekly-index-per-exchange rule. Update these when they roll over.
UNDERLYINGS: list[tuple[str, str, date, int]] = [
    ("NIFTY", "NIFTY 50", date(2026, 8, 18), 50),
    ("BANKNIFTY", "NIFTY BANK", date(2026, 8, 25), 100),
]


def _option_symbol(underlying: str, expiry: date, strike: int, option_type: str) -> str:
    """TrueData's real option-symbol convention (confirmed live 2026-08-17
    from the installed SDK's own TD_chain.py): plain underlying name, no
    space, expiry as %y%m%d, then the strike, then a CE/PE suffix — a
    different convention from Shoonya's own P/C-suffix format, so don't
    reuse any Shoonya symbol-construction code here.
    """
    return f"{underlying}{expiry.strftime('%y%m%d')}{strike}{option_type}"


def _save_df(df: object, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if df is None or df.empty:  # type: ignore[union-attr]
        return 0
    df.to_csv(path, index=False)  # type: ignore[union-attr]
    return len(df)  # type: ignore[arg-type]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration", default="15 D", help="TD_hist duration string, e.g. '15 D', '5 D'"
    )
    parser.add_argument(
        "--strikes",
        type=int,
        default=10,
        help=(
            "Strikes on each side of ATM to fetch "
            "(default 10 -> 21 strikes x 2 = 42 contracts per underlying)"
        ),
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

    df = td_hist.get_historic_data("INDIA VIX", duration=args.duration, bar_size="1 min")
    n = _save_df(df, DATA_DIR / "underlyings" / "INDIA_VIX_1min.csv")
    print(f"INDIA VIX: {n} rows saved")

    for underlying, td_symbol, expiry, strike_step in UNDERLYINGS:
        df = td_hist.get_historic_data(td_symbol, duration=args.duration, bar_size="1 min")
        n = _save_df(df, DATA_DIR / "underlyings" / f"{underlying}_1min.csv")
        print(f"{underlying}: {n} underlying rows saved")
        if df is None or df.empty:
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
                n = _save_df(
                    opt_df,
                    DATA_DIR / "options" / underlying / expiry.isoformat() / f"{symbol}.csv",
                )
                if n:
                    fetched += 1
                    print(f"  {symbol}: {n} rows")
                else:
                    print(f"  {symbol}: no data (not listed / no trades in window)")
        print(f"  {underlying}: {fetched}/{len(strikes) * 2} option contracts had real data\n")

    print(f"Done. Data saved under {DATA_DIR}")


if __name__ == "__main__":
    main()
