"""One-off: pull real Shoonya TPSeries 1-min bars for a single trading day
(today, by default) -- NIFTY spot index, India VIX, and a wide strike range
of NFO option contracts -- to feed run_backtest.py for a same-day
backtest-vs-live-paper-trading validation run.

Deliberately not part of the app -- one-off data-pull script, same
"local-only, deploy/run on production via SSH for the cached Shoonya
session, then delete" convention as fetch_shoonya_futures_history.py.

Usage (run on the box with an active Shoonya session, e.g. production):
    python scripts/fetch_today_replay_data.py --use-cached \\
        --date 2026-08-26 --out-dir /tmp/today_replay \\
        --strikes "23800,23850,...,25200" --index-token 26000 --vix-token 26017
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date as date_cls
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import get_settings  # noqa: E402
from app.modules.broker_adapter.shoonya.rest_client import ShoonyaRestClient  # noqa: E402
from app.modules.broker_adapter.shoonya.session_cache import (  # noqa: E402
    get_cached_shoonya_session,
)

IST = ZoneInfo("Asia/Kolkata")


def _fetch_one(
    client: ShoonyaRestClient, uid: str, exchange: str, token: str, day: date_cls
) -> list[dict]:
    start = datetime.combine(day, time(0, 0), tzinfo=IST)
    end = datetime.combine(day, time(23, 59, 59), tzinfo=IST)
    try:
        rows = client.get_time_price_series(
            uid, exchange, token, int(start.timestamp()), int(end.timestamp()), interval_minutes=1
        )
    except Exception as exc:  # noqa: BLE001 -- one-off script, log and continue
        print(f"    ERROR: {exc}")
        return []
    return sorted(rows, key=lambda r: int(r["ssboe"]))


def _write_csv(rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume", "oi"])
        for r in rows:
            ts = datetime.fromtimestamp(int(r["ssboe"]), tz=IST)
            w.writerow(
                [
                    ts.strftime("%Y-%m-%d %H:%M:%S"),
                    r.get("into", ""),
                    r.get("inth", ""),
                    r.get("intl", ""),
                    r.get("intc", ""),
                    r.get("intv", 0),
                    r.get("oi", 0),
                ]
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--use-cached", action="store_true", required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD, IST calendar day to fetch")
    parser.add_argument("--out-dir", default="/tmp/today_replay")
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--expiry", required=True, help="YYYY-MM-DD option expiry")
    parser.add_argument("--index-token", default="26000")
    parser.add_argument("--vix-token", default="26017")
    parser.add_argument(
        "--contracts",
        required=True,
        help="comma-separated strike:option_type:broker_token triples, "
        "e.g. '24500:CE:47005,24500:PE:47006'",
    )
    args = parser.parse_args()

    day = date_cls.fromisoformat(args.date)
    expiry = date_cls.fromisoformat(args.expiry)
    out_dir = Path(args.out_dir)

    settings = get_settings().shoonya
    auth = get_cached_shoonya_session()
    if auth is None:
        raise SystemExit("No cached Shoonya session found -- reconnect Shoonya in the app first.")
    print(f"Reusing cached Shoonya session -- account_id={auth.account_id}")
    client = ShoonyaRestClient(settings.api_host, auth.session_token)

    print(f"\n=== {args.underlying} spot index (token {args.index_token}) ===")
    idx_rows = _fetch_one(client, auth.account_id, "NSE", args.index_token, day)
    print(f"  {len(idx_rows)} rows")
    if idx_rows:
        _write_csv(idx_rows, out_dir / "underlyings" / f"{args.underlying}_1min.csv")

    print(f"\n=== India VIX (token {args.vix_token}) ===")
    vix_rows = _fetch_one(client, auth.account_id, "NSE", args.vix_token, day)
    print(f"  {len(vix_rows)} rows")
    if vix_rows:
        _write_csv(vix_rows, out_dir / "underlyings" / "INDIA_VIX_1min_today.csv")

    contracts = []
    for spec in args.contracts.split(","):
        strike_s, opt_type, token = spec.split(":")
        contracts.append((int(float(strike_s)), opt_type.strip().upper(), token.strip()))

    print(f"\n=== {len(contracts)} option contracts, expiry {expiry.isoformat()} ===")
    fetched = 0
    for strike, opt_type, token in contracts:
        symbol = f"{args.underlying}{expiry.strftime('%y%m%d')}{strike}{opt_type}"
        rows = _fetch_one(client, auth.account_id, "NFO", token, day)
        if rows:
            fetched += 1
            leg_dir = out_dir / "options_1min_past" / args.underlying / expiry.isoformat()
            _write_csv(rows, leg_dir / f"{symbol}.csv")
        print(f"  {symbol} (token {token}): {len(rows)} rows")

    print(f"\n{fetched}/{len(contracts)} contracts had real data. Written under {out_dir}")


if __name__ == "__main__":
    main()
