"""One-off, standalone probe: does Alice Blue's historical chart API
(`POST {api_host}/open-api/od/ChartAPIService/api/chart/history`) actually
return real 1-min NIFTY 50 index data for an *older* window (well past
TrueData trial's 15-day cap), matching Alice's own docs claim of "2 years of
historical data" for the NSE segment? Standalone and read-only — does not
touch the app DB, does not write to `.alice_blue_session_cache.json` unless
`--cache` is passed, never used by anything else in the app.

Two-step usage (OAuth login can't be scripted — must be the user's own
browser):

    1. python scripts/probe_alice_blue_historical.py --print-url
       -> open the printed URL in your own browser, log in, then copy the
          FULL URL your browser ends up on after Alice Blue redirects you
          (it will 404/401 on the OCI callback page — that's fine, the
          query params are all this needs).

    2. python scripts/probe_alice_blue_historical.py --callback-url "<pasted URL>"
       -> exchanges the code for a session, then requests NIFTY 50 (NSE
          index token 26000) 1-min bars for a window ~90-120 days back and
          prints exactly what comes back (row count + first/last timestamp),
          so we can tell real historical depth from a trial-style empty/short
          response without guessing.
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-url", action="store_true")
    parser.add_argument("--callback-url", default=None)
    parser.add_argument(
        "--use-cached", action="store_true",
        help="Reuse the session this script already cached to disk from a prior "
        "--callback-url run, instead of requiring a fresh authCode.",
    )
    parser.add_argument(
        "--days-back-start", type=int, default=120,
        help="How many days before today the probe window starts.",
    )
    parser.add_argument(
        "--days-back-end", type=int, default=90,
        help="How many days before today the probe window ends.",
    )
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
            raise SystemExit("No cached session on disk — run with --callback-url first.")
        session = cached
        print(f"Reusing cached session — client_id={session.client_id}")
    else:
        if not args.callback_url:
            raise SystemExit(
                "Pass --print-url first, then --callback-url \"<pasted URL>\" "
                "(or --use-cached to reuse a session cached by a prior run)"
            )
        parsed = urlparse(args.callback_url)
        qs = parse_qs(parsed.query)
        try:
            auth_code = qs["authCode"][0]
            user_id = qs["userId"][0]
        except KeyError as exc:
            raise SystemExit(
                f"Pasted URL is missing authCode/userId query params: {args.callback_url}"
            ) from exc

        try:
            session = exchange_for_session(settings, auth_code, user_id)
        except AliceBlueAuthError as exc:
            raise SystemExit(f"Login exchange failed: {exc}") from exc
        set_alice_blue_session(session)
        print(f"Logged in OK, cached to disk — client_id={session.client_id}")

    end = datetime.now() - timedelta(days=args.days_back_end)
    start = datetime.now() - timedelta(days=args.days_back_start)
    from_ms = int(start.timestamp() * 1000)
    to_ms = int(end.timestamp() * 1000)
    print(
        f"Requesting NIFTY 50 (NSE index token 26000) 1-min bars for "
        f"{start.date()} .. {end.date()} ..."
    )

    client = httpx.Client(timeout=15.0, proxy=settings.auth_proxy or None)
    try:
        response = client.post(
            f"{settings.api_host}/open-api/od/ChartAPIService/api/chart/history",
            json={
                "token": "26000",
                "exchange": "NSE::index",
                "resolution": "1",
                "from": str(from_ms),
                "to": str(to_ms),
            },
            headers={"Authorization": f"Bearer {session.user_session}"},
        )
    finally:
        client.close()

    print(f"HTTP {response.status_code}")
    body = response.text
    print(body[:2000])

    try:
        data = response.json()
    except ValueError:
        return
    candles = data.get("candles") or data.get("result") or data.get("data")
    if isinstance(candles, list):
        print(f"\n{len(candles)} candles returned")
        if candles:
            print("first:", candles[0])
            print("last:", candles[-1])


if __name__ == "__main__":
    main()
