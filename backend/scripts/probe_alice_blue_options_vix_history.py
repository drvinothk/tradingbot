"""Probe: does Alice Blue's historical chart API return 1-min data for (a)
an NFO option contract and (b) India VIX, and how far back? Same endpoint
`probe_alice_blue_historical.py` already confirmed for the NIFTY 50 index
(2 yrs, gap-free) — this checks whether that same depth holds for options
and VIX, since neither has been live-tested before.

Usage (same OAuth dance as probe_alice_blue_historical.py):
    python scripts/probe_alice_blue_options_vix_history.py --print-url
    python scripts/probe_alice_blue_options_vix_history.py --callback-url "<pasted URL>"
    python scripts/probe_alice_blue_options_vix_history.py --use-cached
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.modules.market_data.providers.alice_blue_auth import (  # noqa: E402
    AliceBlueAuthError,
    AliceBlueSession,
    build_authorize_url,
    exchange_for_session,
)
from app.modules.market_data.providers.alice_blue_session import (  # noqa: E402
    get_alice_blue_session,
    set_alice_blue_session,
)

NFO_URL = "https://v2api.aliceblueonline.com/restpy/static/contract_master/V2/NFO"
INDICES_URL = "https://v2api.aliceblueonline.com/restpy/static/contract_master/V2/INDICES"


def _pick_nifty_option_token() -> tuple[str, str]:
    """Real, currently-listed NIFTY option token+symbol, farthest-dated
    available (longest own listed life to probe against)."""
    resp = httpx.get(NFO_URL, timeout=15.0)
    resp.raise_for_status()
    rows = resp.json().get("NFO", [])
    nifty_opts = [
        r for r in rows
        if r.get("symbol") == "NIFTY" and r.get("option_type") in ("CE", "PE")
    ]
    nifty_opts.sort(key=lambda r: r.get("expiry_date", ""), reverse=True)
    row = nifty_opts[0]
    return str(row["token"]), row.get("trading_symbol", "?")


def _pick_vix_token() -> tuple[str, str] | None:
    resp = httpx.get(INDICES_URL, timeout=15.0)
    resp.raise_for_status()
    body = resp.json()
    for row in body.get("NSE", []):
        if "VIX" in str(row.get("symbol", "")).upper():
            return str(row["token"]), row["symbol"]
    return None


def _query(client: httpx.Client, api_host: str, token: str, exchange: str,
           bearer: str, days_back_start: int, days_back_end: int) -> None:
    end = datetime.now() - timedelta(days=days_back_end)
    start = datetime.now() - timedelta(days=days_back_start)
    from_ms = int(start.timestamp() * 1000)
    to_ms = int(end.timestamp() * 1000)
    print(f"  window: {start.date()} .. {end.date()}  token={token} exchange={exchange}")
    response = client.post(
        f"{api_host}/open-api/od/ChartAPIService/api/chart/history",
        json={
            "token": token, "exchange": exchange, "resolution": "1",
            "from": str(from_ms), "to": str(to_ms),
        },
        headers={"Authorization": f"Bearer {bearer}"},
    )
    print(f"  HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        print("  body:", response.text[:500])
        return
    candles = data.get("candles") or data.get("result") or data.get("data")
    if isinstance(candles, list):
        print(f"  {len(candles)} candles")
        if candles:
            print(f"  first: {candles[0]}")
            print(f"  last:  {candles[-1]}")
    else:
        print("  body:", str(data)[:500])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-url", action="store_true")
    parser.add_argument("--callback-url", default=None)
    parser.add_argument("--use-cached", action="store_true")
    args = parser.parse_args()

    settings = get_settings().alice_blue
    missing = settings.missing_required_fields()
    if missing:
        raise SystemExit(f"Missing Alice Blue settings: {missing}")

    if args.print_url:
        print(build_authorize_url(settings))
        return

    session: AliceBlueSession
    if args.use_cached:
        cached = get_alice_blue_session()
        if cached is None:
            raise SystemExit("No cached session — run with --callback-url first.")
        session = cached
        print(f"Reusing cached session — client_id={session.client_id}")
    else:
        if not args.callback_url:
            raise SystemExit('Pass --print-url first, then --callback-url "<pasted URL>"')
        qs = parse_qs(urlparse(args.callback_url).query)
        try:
            auth_code = qs["authCode"][0]
            user_id = qs["userId"][0]
        except KeyError as exc:
            raise SystemExit(f"Pasted URL missing authCode/userId: {args.callback_url}") from exc
        try:
            session = exchange_for_session(settings, auth_code, user_id)
        except AliceBlueAuthError as exc:
            raise SystemExit(f"Login exchange failed: {exc}") from exc
        set_alice_blue_session(session)
        print(f"Logged in OK, cached — client_id={session.client_id}")

    print("\nLooking up a real NIFTY NFO option token...")
    opt_token, opt_symbol = _pick_nifty_option_token()
    print(f"  -> {opt_symbol} (token {opt_token})")

    print("\nLooking up India VIX token...")
    vix = _pick_vix_token()
    print(f"  -> {vix}" if vix else "  -> NOT FOUND in Alice Blue INDICES list")

    client = httpx.Client(timeout=15.0, proxy=settings.auth_proxy or None)
    try:
        print(f"\n=== NFO option {opt_symbol}: last 10 days (sanity check) ===")
        _query(client, settings.api_host, opt_token, "NFO", session.user_session, 10, 0)

        print(f"\n=== NFO option {opt_symbol}: 90-60 days back ===")
        _query(client, settings.api_host, opt_token, "NFO", session.user_session, 90, 60)

        print(f"\n=== NFO option {opt_symbol}: 400-370 days back (>1yr) ===")
        _query(client, settings.api_host, opt_token, "NFO", session.user_session, 400, 370)

        if vix:
            vix_token, _ = vix
            print("\n=== India VIX: last 10 days (sanity check) ===")
            _query(client, settings.api_host, vix_token, "NSE::index", session.user_session, 10, 0)

            print("\n=== India VIX: 400-370 days back (>1yr) ===")
            _query(
                client, settings.api_host, vix_token, "NSE::index", session.user_session, 400, 370
            )

            print("\n=== India VIX: 700-670 days back (~2yr) ===")
            _query(
                client, settings.api_host, vix_token, "NSE::index", session.user_session, 700, 670
            )
    finally:
        client.close()


if __name__ == "__main__":
    main()
