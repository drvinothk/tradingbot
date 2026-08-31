"""Probe: does Shoonya's TPSeries endpoint return historical 1-min data for
NFO options and futures, and how far back? `get_price_history` in
`ShoonyaBrokerAdapter` only ever exercises this for NSE index tokens
(NIFTY/BANKNIFTY spot) — option/futures depth via TPSeries has never been
tested. Read-only (TPSeries only), same OAuth browser-login flow as any
normal Shoonya connect.

Usage:
    python scripts/probe_shoonya_options_futures_history.py --print-url
    python scripts/probe_shoonya_options_futures_history.py --callback-url "<pasted URL>"
    python scripts/probe_shoonya_options_futures_history.py --use-cached
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import csv
import io
import zipfile

import httpx  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.modules.broker_adapter.shoonya.auth import (  # noqa: E402
    ShoonyaAuthError,
    build_authorize_url,
    exchange_code_for_token,
)
from app.modules.broker_adapter.shoonya.rest_client import ShoonyaRestClient  # noqa: E402
from app.modules.broker_adapter.shoonya.scrip_master import (  # noqa: E402
    _NFO_SCRIP_MASTER_URL,
    parse_shoonya_scrip_expiry,
)
from app.modules.broker_adapter.shoonya.session_cache import (  # noqa: E402
    get_cached_shoonya_session,
)


def _pick_tokens() -> tuple[tuple[str, str], tuple[str, str]]:
    """Returns ((option_token, option_symbol), (future_token, future_symbol))
    for real, currently-listed NIFTY contracts, from the public no-auth
    NFO_symbols.txt.zip scrip master. Parses OPTIDX+FUTIDX rows directly
    (not via `parse_nfo_scrip_master`, which only keeps OPTIDX)."""
    resp = httpx.get(_NFO_SCRIP_MASTER_URL, timeout=30.0)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
        raw_text = zf.read(names[0]).decode("utf-8", errors="replace")

    opts: list[tuple[str, str, str]] = []  # (expiry_str, token, tsym)
    futs: list[tuple[str, str, str]] = []
    for row in csv.DictReader(io.StringIO(raw_text)):
        if row.get("Symbol", "").strip().upper() != "NIFTY":
            continue
        instrument = row.get("Instrument", "").strip().upper()
        token = row.get("Token", "").strip()
        tsym = row.get("TradingSymbol", "").strip()
        expiry_raw = row.get("Expiry", "").strip()
        if not token or not tsym or not expiry_raw:
            continue
        if instrument == "OPTIDX":
            opts.append((expiry_raw, token, tsym))
        elif instrument == "FUTIDX":
            futs.append((expiry_raw, token, tsym))

    opts.sort(key=lambda r: parse_shoonya_scrip_expiry(r[0]))
    futs.sort(key=lambda r: parse_shoonya_scrip_expiry(r[0]))
    if not opts:
        raise SystemExit("No NIFTY OPTIDX rows found in scrip master")
    if not futs:
        raise SystemExit("No NIFTY FUTIDX rows found in scrip master")
    opt = opts[0]
    fut = futs[0]
    return (opt[1], opt[2]), (fut[1], fut[2])


def _query(client: ShoonyaRestClient, uid: str, exchange: str, token: str, symbol: str,
           days_back_start: int, days_back_end: int, label: str) -> None:
    end = datetime.now() - timedelta(days=days_back_end)
    start = datetime.now() - timedelta(days=days_back_start)
    print(f"{label} [{symbol}] window {start.date()}..{end.date()}:", end=" ")
    try:
        rows = client.get_time_price_series(
            uid, exchange, token, int(start.timestamp()), int(end.timestamp()), interval_minutes=1
        )
    except Exception as exc:  # noqa: BLE001 -- probe script, report and continue
        print(f"ERROR: {exc}")
        return
    print(f"{len(rows)} rows", end="")
    if rows:
        print(f"  first={rows[0]}  last={rows[-1]}")
    else:
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-url", action="store_true")
    parser.add_argument("--callback-url", default=None)
    parser.add_argument("--use-cached", action="store_true")
    args = parser.parse_args()

    settings = get_settings().shoonya
    missing = settings.missing_required_fields()
    if missing:
        raise SystemExit(f"Missing Shoonya settings: {missing}")

    if args.print_url:
        print(build_authorize_url(settings))
        return

    if args.use_cached:
        auth = get_cached_shoonya_session()
        if auth is None:
            raise SystemExit(
                "No cached Shoonya session found — reconnect Shoonya in the app first, "
                "or use --print-url/--callback-url instead."
            )
        print(f"Reusing cached Shoonya session — account_id={auth.account_id}")
    else:
        if not args.callback_url:
            raise SystemExit(
                'Pass --print-url first, then --callback-url "<pasted URL>", or --use-cached'
            )

        qs = parse_qs(urlparse(args.callback_url).query)
        try:
            code = qs["code"][0]
        except KeyError as exc:
            raise SystemExit(f"Pasted URL missing 'code' query param: {args.callback_url}") from exc

        try:
            oauth_session = exchange_code_for_token(settings, code)
        except ShoonyaAuthError as exc:
            raise SystemExit(f"Login exchange failed: {exc}") from exc
        auth = oauth_session.auth_result
        print(f"Logged in OK — account_id={auth.account_id}")

    print("\nLooking up real NIFTY option + futures tokens (public scrip master)...")
    (opt_token, opt_symbol), (fut_token, fut_symbol) = _pick_tokens()
    print(f"  option:  {opt_symbol} (token {opt_token})")
    print(f"  futures: {fut_symbol} (token {fut_token})")

    client = ShoonyaRestClient(settings.api_host, auth.session_token)
    uid = auth.account_id
    try:
        print(f"\n=== Option {opt_symbol}: last 5 days (sanity check) ===")
        _query(client, uid, "NFO", opt_token, opt_symbol, 5, 0, "option")

        print(f"\n=== Option {opt_symbol}: 40-25 days back ===")
        _query(client, uid, "NFO", opt_token, opt_symbol, 40, 25, "option")

        print(f"\n=== Option {opt_symbol}: 400-370 days back (>1yr) ===")
        _query(client, uid, "NFO", opt_token, opt_symbol, 400, 370, "option")

        print(f"\n=== Futures {fut_symbol}: last 5 days (sanity check) ===")
        _query(client, uid, "NFO", fut_token, fut_symbol, 5, 0, "futures")

        print(f"\n=== Futures {fut_symbol}: 60-40 days back ===")
        _query(client, uid, "NFO", fut_token, fut_symbol, 60, 40, "futures")

        print(f"\n=== Futures {fut_symbol}: 400-370 days back (>1yr) ===")
        _query(client, uid, "NFO", fut_token, fut_symbol, 400, 370, "futures")
    finally:
        pass


if __name__ == "__main__":
    main()
