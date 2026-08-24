"""Fetches real, continuous 1-min NIFTY 50 / NIFTY BANK **index** history
from Alice Blue's historical chart API — the fix for the underlying-price
gap that both TrueData's spot feed (~12-15 day trial cap) and the
futures-proxy workaround (real data only ~1 week before each monthly
contract's own expiry) couldn't solve. Confirmed live 2026-08-24 via
`probe_alice_blue_historical.py`: real, gap-free 1-min candles at 90-120
days back, ~2 years back, and ~2.5 years back — matching Alice's own docs
claim of "2 years of historical data" for the NSE segment (index symbols
are NSE, not NFO — NFO/options/futures are current-expiry-only, a separate,
unrelated limitation this script does not touch).

Standalone: reuses whatever Alice Blue session `probe_alice_blue_historical
.py` (or the app's own OAuth login) has already cached to
`config/credentials/.alice_blue_session_cache.json` — does not itself do
any browser-redirect login. Run that probe script (or reconnect via the
app) first if `get_alice_blue_session()` returns `None`.

Chunked in 30-day windows (the exact size already proven to return full,
un-truncated data in the confirming probe) with a polite delay between
requests — this endpoint's real rate limit is unconfirmed, so this
deliberately errs conservative rather than hammering a real broker
account's API. Walks backward from `--to` (default: today) and stops once
`--empty-chunk-tolerance` (default 3) *consecutive* chunks come back empty,
treating that as "reached the real historical boundary" rather than an
isolated market-holiday gap.

Output:
    data/historical/underlyings_alice_1min_past/<underlying>/<window-start>.csv
        — one raw file per fetched 30-day window, exactly as returned
          (resumable: an existing window file is not re-fetched).
    data/historical/underlyings/<underlying>_alice_index_1min.csv
        — all windows concatenated, deduped/sorted by timestamp: the
          continuous series `run_backtest.py --underlying-source
          alice_index` reads.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.modules.market_data.providers.alice_blue_session import (  # noqa: E402
    get_alice_blue_session,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "historical"

# (underlying name, NSE index token) -- confirmed live 2026-08-21, see
# alice_blue_scrip_master.py's own docstring: same tokens Shoonya uses.
UNDERLYING_TOKENS = {"NIFTY": "26000", "BANKNIFTY": "26009"}

WINDOW_DAYS = 30
COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "oi"]


def _fetch_window(
    client: httpx.Client, api_host: str, bearer: str, token: str, start: date, end: date
) -> list[dict]:
    from_ms = int(datetime.combine(start, datetime.min.time()).timestamp() * 1000)
    to_ms = int(datetime.combine(end, datetime.min.time()).timestamp() * 1000)
    response = client.post(
        f"{api_host}/open-api/od/ChartAPIService/api/chart/history",
        json={
            "token": token, "exchange": "NSE::index", "resolution": "1",
            "from": str(from_ms), "to": str(to_ms),
        },
        headers={"Authorization": f"Bearer {bearer}"},
    )
    if response.status_code == 429:
        raise SystemExit(
            "HTTP 429 (rate limited) from Alice Blue's historical API -- stopping rather "
            "than hammering a real broker account. Wait and rerun; already-fetched window "
            "files are skipped automatically."
        )
    response.raise_for_status()
    body = response.json()
    if str(body.get("stat", "")).lower() != "ok":
        return []
    return body.get("result") or []


def _save_window(rows: list[dict], out_path: Path) -> int:
    import csv as _csv

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = _csv.writer(f)
        writer.writerow(COLUMNS)
        for r in rows:
            # Alice stamps each bar at its own closing second (e.g. "...09:15:59"),
            # not the minute start every other CSV in this codebase uses -- floor
            # to "HH:MM:00" so run_backtest.py's %Y-%m-%d %H:%M:%S parser and its
            # minute-bucket lookups line up with every other underlying source.
            ts = r["time"][:16] + ":00"
            writer.writerow([ts, r["open"], r["high"], r["low"], r["close"], r["volume"], 0])
    return len(rows)


def _stitch_continuous(window_dir: Path, out_path: Path) -> int:
    """Plain-`csv` dedupe/sort — deliberately no pandas (this venv excludes
    it on purpose, see module docstring's own note on why). Later window's
    row wins on a same-timestamp collision, same "later data supersedes"
    convention `fetch_truedata_futures_underlying_history.py` already uses.
    """
    import csv as _csv

    by_ts: dict[str, list[str]] = {}
    for csv_path in sorted(window_dir.glob("*.csv")):
        with csv_path.open(newline="") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                by_ts[row["timestamp"]] = [row[c] for c in COLUMNS]
    if not by_ts:
        return 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = _csv.writer(f)
        writer.writerow(COLUMNS)
        for ts in sorted(by_ts):
            writer.writerow(by_ts[ts])
    return len(by_ts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", dest="to_date", type=date.fromisoformat, default=date.today())
    parser.add_argument(
        "--max-years-back", type=float, default=3.2,
        help="Upper bound on how far back to attempt -- Alice's docs claim 2 years; "
        "probed further live, so this pads past that rather than trusting the doc alone.",
    )
    parser.add_argument("--empty-chunk-tolerance", type=int, default=3)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument(
        "--underlyings", nargs="+", default=list(UNDERLYING_TOKENS), choices=list(UNDERLYING_TOKENS)
    )
    args = parser.parse_args()

    session = get_alice_blue_session()
    if session is None:
        raise SystemExit(
            "No cached Alice Blue session -- run scripts/probe_alice_blue_historical.py "
            "with --callback-url first (or reconnect via the app, then copy its cache file)."
        )
    settings = get_settings().alice_blue
    print(f"Using cached session for client_id={session.client_id}")

    earliest = args.to_date - timedelta(days=int(args.max_years_back * 365.25))
    client = httpx.Client(timeout=20.0, proxy=settings.auth_proxy or None)
    try:
        for underlying in args.underlyings:
            token = UNDERLYING_TOKENS[underlying]
            window_dir = DATA_DIR / "underlyings_alice_1min_past" / underlying
            window_dir.mkdir(parents=True, exist_ok=True)
            print(f"\n=== {underlying} (token {token}) ===")

            window_end = args.to_date
            consecutive_empty = 0
            total_rows = 0
            while window_end > earliest and consecutive_empty < args.empty_chunk_tolerance:
                window_start = max(window_end - timedelta(days=WINDOW_DAYS), earliest)
                out_file = window_dir / f"{window_start.isoformat()}.csv"
                if out_file.is_file():
                    n = sum(1 for _ in out_file.open()) - 1
                    print(f"  {window_start} .. {window_end}: already fetched ({n} rows), skipping")
                    consecutive_empty = 0 if n > 0 else consecutive_empty + 1
                    total_rows += max(n, 0)
                    window_end = window_start
                    continue

                rows = _fetch_window(
                    client, settings.api_host, session.user_session, token,
                    window_start, window_end,
                )
                n = _save_window(rows, out_file)
                total_rows += n
                consecutive_empty = 0 if n > 0 else consecutive_empty + 1
                print(
                    f"  {window_start} .. {window_end}: {n} rows"
                    + ("  <-- empty" if n == 0 else "")
                )
                window_end = window_start
                time.sleep(args.sleep_seconds)

            if consecutive_empty >= args.empty_chunk_tolerance:
                print(
                    f"  Stopped: {consecutive_empty} consecutive empty windows "
                    f"(real historical boundary reached around {window_end})"
                )
            print(f"  Total raw rows fetched this run: {total_rows}")

            continuous_out = DATA_DIR / "underlyings" / f"{underlying}_alice_index_1min.csv"
            stitched = _stitch_continuous(window_dir, continuous_out)
            print(f"  Stitched continuous series -> {continuous_out} ({stitched} rows)")
    finally:
        client.close()


if __name__ == "__main__":
    main()
