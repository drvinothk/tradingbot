"""Fetch near-expiry-week 1-min option data via Shoonya `TPSeries` and merge
it into `backtest_engine`'s `options_1min_past` archive.

**Timing is the entire constraint here, confirmed live 2026-09-05**: Shoonya
`TPSeries` returns real 1-min history for a currently-listed option's full
life (e.g. a BANKNIFTY monthly contract 45+ days back), but the instant a
contract expires and drops off the live scrip master, `TPSeries` rejects its
token outright (`"invalid input[Token not prsent]"`) -- even a token we
already have cached in our own `option_contracts` table from when it was
live. There is no retroactive backfill path. This script must therefore run
*before* each contract's own expiry, which is what the accompanying systemd
timer is for -- see `backtest_engine/README.md`'s own note on this job.

Reads real, already-synced strikes/tokens straight from this app's own
production `option_contracts` table (no separate SearchScrip lookup needed
-- production's own daily sync already keeps a real ATM-centered band
active), and the live Shoonya session from disk cache (same file the app
itself uses -- no separate login).

Idempotent: re-running mid-week (to capture a partial week ahead of the
scheduled run, or as a manual top-up) merges into any existing archive file
for that contract by timestamp -- newer rows overwrite, nothing duplicates.

Usage:
    python scripts/fetch_shoonya_near_expiry_options.py --underlying NIFTY
    python scripts/fetch_shoonya_near_expiry_options.py --underlying BANKNIFTY --dry-run
    python scripts/fetch_shoonya_near_expiry_options.py --underlying both \\
        --archive-root "/home/ubuntu/backtest_engine/backend/data/historical/options_1min_past"
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import get_settings  # noqa: E402
from app.core.db.session import session_scope  # noqa: E402
from app.domain.market.models import Instrument, OptionContract, PriceBar  # noqa: E402
from app.modules.broker_adapter.shoonya.rest_client import ShoonyaRestClient  # noqa: E402
from app.modules.broker_adapter.shoonya.session_cache import (  # noqa: E402
    get_cached_shoonya_session,
)
from app.modules.market_data.tick_plausibility import (  # noqa: E402
    MAX_PLAUSIBLE_OPTION_PREMIUM,
)

# Wed->Tue near-expiry week, matching run_backtest.py's own
# `--near-expiry-days 6` convention exactly (expiry_date - 6 days = the
# Wednesday that starts that trading week).
NEAR_EXPIRY_DAYS = 6

# No default: on the OCI box, trading-bot/ and backtest_engine/ are siblings
# under /home/ubuntu/, but locally backtest_engine/ nests *inside* the main
# repo -- a single guessed relative path would be right on one and silently
# wrong on the other. --archive-root is required so every invocation (the
# systemd service included) states its target explicitly.


def _latest_spot(db, underlying: str) -> float | None:
    """Latest close from this app's own `price_bars` (60s) for the
    underlying index -- avoids needing a fresh broker quote call (a new
    `ShoonyaBrokerAdapter` instance here would have an empty in-memory
    token cache and can't resolve one standalone)."""
    instrument = db.query(Instrument).filter(Instrument.symbol == underlying).one_or_none()
    if instrument is None:
        return None
    bar = (
        db.query(PriceBar)
        .filter(PriceBar.instrument_id == instrument.id, PriceBar.timeframe == "60s")
        .order_by(PriceBar.bucket_start.desc())
        .first()
    )
    return float(bar.close) if bar is not None else None


def _active_near_expiry_contracts(
    underlying: str, today: date, strikes_each_side: int
) -> tuple[date, list[OptionContract]]:
    """Returns (expiry_date, contracts) for the nearest active expiry >=
    today, narrowed to the `strikes_each_side` strikes nearest the latest
    known spot (production's own `is_active` band is much wider than what
    any strategy actually trades -- confirmed live 2026-09-05, 454 "active"
    rows for a single NIFTY weekly, mostly stale/never-cleaned-up structural
    rows from routine chain syncs, not real ATM-relevant strikes). Fetching
    all of them would burn ~450 TPSeries calls per underlying for strikes
    that were never near the money, most returning "no data"."""
    with session_scope() as db:
        instrument = db.query(Instrument).filter(Instrument.symbol == underlying).one_or_none()
        if instrument is None:
            raise SystemExit(f"No instrument row for {underlying!r} -- has it ever been synced?")
        rows = (
            db.query(OptionContract)
            .filter(
                OptionContract.instrument_id == instrument.id,
                OptionContract.is_active.is_(True),
                OptionContract.expiry_date >= today,
                OptionContract.broker_token != "",
            )
            .order_by(OptionContract.expiry_date.asc())
            .all()
        )
        if not rows:
            return today, []
        nearest = rows[0].expiry_date
        chain = [r for r in rows if r.expiry_date == nearest]

        spot = _latest_spot(db, underlying)
        if spot is not None:
            distinct_strikes = sorted({float(r.strike) for r in chain}, key=lambda s: abs(s - spot))
            keep_strikes = set(distinct_strikes[: strikes_each_side * 2])
            chain = [r for r in chain if float(r.strike) in keep_strikes]
        # else: no spot known (e.g. never ingested) -- fall back to fetching
        # everything active rather than guessing a window; slower but safe.

        # Detach the plain fields we need before the session closes.
        return nearest, [
            OptionContract(
                symbol=r.symbol, broker_token=r.broker_token, strike=r.strike,
                option_type=r.option_type, expiry_date=r.expiry_date,
            )
            for r in chain
        ]


def _archive_filename(underlying: str, expiry: date, strike: float, option_type: str) -> str:
    """`{underlying}{expiry:%y%m%d}{strike}{CE|PE}` -- must match
    run_backtest.py's `_parse_option_symbol` exactly, byte for byte."""
    strike_str = str(int(strike)) if float(strike).is_integer() else str(strike)
    return f"{underlying}{expiry.strftime('%y%m%d')}{strike_str}{option_type}"


def _parse_tpseries_row(row: dict) -> tuple[datetime, float, float, float, float, int, int] | None:
    """Shoonya TPSeries row -> (ts, open, high, low, close, volume, oi).
    `intv` is the bar's own volume (not `v`, the cumulative session total --
    confirmed live: `v` only ever grows across a whole day's rows). Returns
    None for a row missing required fields (defensive; not observed live)."""
    try:
        ts = datetime.strptime(row["time"], "%d-%m-%Y %H:%M:%S")
        o, h, lo, c = float(row["into"]), float(row["inth"]), float(row["intl"]), float(row["intc"])
        vol = int(float(row.get("intv", 0)))
        oi = int(float(row.get("oi", 0)))
    except (KeyError, ValueError):
        return None
    return ts, o, h, lo, c, vol, oi


def _is_plausible_bar(o: float, h: float, lo: float, c: float) -> bool:
    hi = MAX_PLAUSIBLE_OPTION_PREMIUM
    return (
        0 < o <= hi and 0 < h <= hi and 0 < lo <= hi and 0 < c <= hi
        and lo <= o <= h and lo <= c <= h
    )


def _merge_and_write(
    path: Path, new_rows: list[tuple[datetime, float, float, float, float, int, int]]
) -> int:
    """Merges new_rows into any existing CSV at `path` by timestamp (new
    values win on overlap), sorts ascending, writes back. Returns the total
    row count after merge."""
    by_ts: dict[datetime, tuple] = {}
    if path.exists():
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                ts = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
                by_ts[ts] = (
                    ts, float(r["open"]), float(r["high"]), float(r["low"]),
                    float(r["close"]), int(r["volume"]), int(r["oi"]),
                )
    for row in new_rows:
        by_ts[row[0]] = row
    ordered = sorted(by_ts.values(), key=lambda r: r[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume", "oi"])
        for ts, o, h, lo, c, vol, oi in ordered:
            writer.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"), o, h, lo, c, vol, oi])
    return len(ordered)


def fetch_underlying(
    underlying: str, client: ShoonyaRestClient, uid: str, archive_root: Path,
    *, dry_run: bool, near_expiry_days: int = NEAR_EXPIRY_DAYS, strikes_each_side: int = 15,
) -> None:
    today = datetime.now().date()
    expiry, contracts = _active_near_expiry_contracts(underlying, today, strikes_each_side)
    sep = "=" * 70
    print(f"\n{sep}\n{underlying} -- expiry {expiry} -- {len(contracts)} contracts\n{sep}")
    if not contracts:
        print("  No active near-term contracts with a real broker_token -- nothing to fetch.")
        return

    near_expiry_start = expiry - timedelta(days=near_expiry_days)
    if near_expiry_start > today:
        # e.g. BANKNIFTY's monthly expiry is weeks away -- its near-expiry
        # week hasn't started yet. Correct to no-op here (a Tuesday-only
        # timer shared with NIFTY's weekly cadence will hit this on every
        # non-expiry Tuesday for BANKNIFTY) rather than firing a stack of
        # TPSeries calls for a window that hasn't happened yet, which
        # Shoonya correctly -- but unhelpfully -- reports as "no data" per
        # contract instead of one clear reason.
        days_away = (near_expiry_start - today).days
        print(f"  Near-expiry week starts in {days_away}d ({expiry}) -- nothing yet.")
        return

    window_start = datetime.combine(near_expiry_start, dt_time(0, 0))
    window_end = min(
        datetime.combine(expiry, dt_time(23, 59, 59)),
        datetime.now(),
    )
    print(f"  window: {window_start} .. {window_end}")

    dest_dir = archive_root / underlying / expiry.isoformat()
    total_written = 0
    total_dropped = 0
    for contract in sorted(contracts, key=lambda c: (c.strike, c.option_type)):
        try:
            raw_rows = client.get_time_price_series(
                uid, "NFO", contract.broker_token,
                int(window_start.timestamp()), int(window_end.timestamp()),
                interval_minutes=1,
            )
        except Exception as exc:  # noqa: BLE001 -- report and continue to the next contract
            print(f"  {contract.symbol}: ERROR {exc}")
            continue

        parsed = [p for p in (_parse_tpseries_row(r) for r in raw_rows) if p is not None]
        plausible = [p for p in parsed if _is_plausible_bar(p[1], p[2], p[3], p[4])]
        dropped = len(parsed) - len(plausible)
        total_dropped += dropped

        filename = _archive_filename(
            underlying, expiry, float(contract.strike), contract.option_type
        )
        dest_path = dest_dir / f"{filename}.csv"
        if dry_run:
            total_written += len(plausible)
            print(f"  {contract.symbol}: {len(plausible)} plausible rows "
                  f"({dropped} dropped) -> would write {dest_path}")
            continue

        total_after_merge = _merge_and_write(dest_path, plausible)
        total_written += len(plausible)
        print(f"  {contract.symbol}: +{len(plausible)} rows ({dropped} dropped) "
              f"-> {dest_path.name} now has {total_after_merge} rows total")

    print(f"\n  {underlying} summary: {total_written} rows fetched this run, "
          f"{total_dropped} implausible bars dropped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--underlying", choices=["NIFTY", "BANKNIFTY", "both"], default="both")
    parser.add_argument("--archive-root", type=Path, required=True,
                         help="Path to backtest_engine's options_1min_past/ directory.")
    parser.add_argument("--near-expiry-days", type=int, default=NEAR_EXPIRY_DAYS)
    parser.add_argument("--strikes-each-side", type=int, default=15,
                         help="Nearest-to-spot strikes to fetch, each side of ATM (default 15).")
    parser.add_argument(
        "--dry-run", action="store_true", help="Fetch and QC but don't write any files."
    )
    args = parser.parse_args()

    settings = get_settings().shoonya
    auth = get_cached_shoonya_session()
    if auth is None:
        raise SystemExit(
            "No cached Shoonya session -- this needs the same session the live app "
            "uses (log in via the app first; this script never opens its own OAuth flow)."
        )
    print(f"Reusing cached Shoonya session -- account_id={auth.account_id}")
    client = ShoonyaRestClient(settings.api_host, auth.session_token)
    uid = auth.account_id

    underlyings = ["NIFTY", "BANKNIFTY"] if args.underlying == "both" else [args.underlying]
    for underlying in underlyings:
        fetch_underlying(
            underlying, client, uid, args.archive_root,
            dry_run=args.dry_run, near_expiry_days=args.near_expiry_days,
            strikes_each_side=args.strikes_each_side,
        )


if __name__ == "__main__":
    main()
