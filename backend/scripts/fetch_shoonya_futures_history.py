"""One-off: pull the real 1-min futures history Shoonya's TPSeries actually
has for the current front-month NIFTY/BANKNIFTY futures contract, using a
cached Shoonya session (reconnect via the app first, then run with
--use-cached). Chunks backward in 30-day windows from today until TPSeries
reports no data twice in a row (taken as "before this contract existed"),
writes a stitched CSV per underlying.

This is a one-off data-pull script, not part of the app — deliberately not
meant to live on any deployed box long-term (see CLAUDE.md's own note on
backtest/analysis scripts staying local-only).

Usage:
    python scripts/fetch_shoonya_futures_history.py --use-cached --out-dir /tmp/shoonya_fut
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.modules.broker_adapter.shoonya.rest_client import ShoonyaRestClient  # noqa: E402
from app.modules.broker_adapter.shoonya.scrip_master import (  # noqa: E402
    _NFO_SCRIP_MASTER_URL,
    parse_shoonya_scrip_expiry,
)
from app.modules.broker_adapter.shoonya.session_cache import (  # noqa: E402
    get_cached_shoonya_session,
)


def _front_future_token(underlying: str) -> tuple[str, str]:
    resp = httpx.get(_NFO_SCRIP_MASTER_URL, timeout=30.0)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
        raw_text = zf.read(names[0]).decode("utf-8", errors="replace")

    futs: list[tuple[str, str, str]] = []
    for row in csv.DictReader(io.StringIO(raw_text)):
        if row.get("Symbol", "").strip().upper() != underlying:
            continue
        if row.get("Instrument", "").strip().upper() != "FUTIDX":
            continue
        token = row.get("Token", "").strip()
        tsym = row.get("TradingSymbol", "").strip()
        expiry_raw = row.get("Expiry", "").strip()
        if not token or not tsym or not expiry_raw:
            continue
        futs.append((expiry_raw, token, tsym))

    if not futs:
        raise SystemExit(f"No {underlying} FUTIDX rows found in scrip master")
    futs.sort(key=lambda r: parse_shoonya_scrip_expiry(r[0]))
    _, token, tsym = futs[0]
    return token, tsym


def _fetch_all(client: ShoonyaRestClient, uid: str, token: str, symbol: str) -> list[dict]:
    """Chunks backward 30 days at a time from now, stopping once two
    consecutive windows both come back empty (past the contract's own
    listing start)."""
    all_rows: dict[str, dict] = {}  # keyed by ssboe to dedupe
    consecutive_empty = 0
    window_end = datetime.now()
    chunk_days = 30
    max_chunks = 24  # up to ~2 years back, generous ceiling

    for i in range(max_chunks):
        window_start = window_end - timedelta(days=chunk_days)
        print(
            f"  [{symbol}] chunk {i}: {window_start.date()} .. {window_end.date()} ...",
            end=" ",
            flush=True,
        )
        try:
            rows = client.get_time_price_series(
                uid,
                "NFO",
                token,
                int(window_start.timestamp()),
                int(window_end.timestamp()),
                interval_minutes=1,
            )
        except Exception as exc:  # noqa: BLE001 -- one-off script, log and stop
            print(f"ERROR: {exc}")
            break

        if not rows:
            consecutive_empty += 1
            print("0 rows")
            if consecutive_empty >= 2:
                print(f"  [{symbol}] two consecutive empty windows -- stopping")
                break
        else:
            consecutive_empty = 0
            print(f"{len(rows)} rows")
            for r in rows:
                all_rows[r["ssboe"]] = r

        window_end = window_start

    return sorted(all_rows.values(), key=lambda r: int(r["ssboe"]))


def _write_csv(rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume", "oi"])
        for r in rows:
            ts = datetime.fromtimestamp(int(r["ssboe"]))
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
    parser.add_argument("--out-dir", default="/tmp/shoonya_fut")
    parser.add_argument("--underlyings", default="NIFTY,BANKNIFTY")
    args = parser.parse_args()

    settings = get_settings().shoonya
    auth = get_cached_shoonya_session()
    if auth is None:
        raise SystemExit("No cached Shoonya session found -- reconnect Shoonya in the app first.")
    print(f"Reusing cached Shoonya session -- account_id={auth.account_id}")

    client = ShoonyaRestClient(settings.api_host, auth.session_token)
    out_dir = Path(args.out_dir)

    for underlying in args.underlyings.split(","):
        underlying = underlying.strip().upper()
        token, symbol = _front_future_token(underlying)
        print(f"\n=== {underlying} front future: {symbol} (token {token}) ===")
        rows = _fetch_all(client, auth.account_id, token, symbol)
        if not rows:
            print(f"  no data at all for {symbol}")
            continue
        first_ts = datetime.fromtimestamp(int(rows[0]["ssboe"]))
        last_ts = datetime.fromtimestamp(int(rows[-1]["ssboe"]))
        print(f"  {len(rows)} total rows, {first_ts} .. {last_ts}")
        out_path = out_dir / f"{underlying}_FUT_shoonya_1min.csv"
        _write_csv(rows, out_path)
        print(f"  written -> {out_path}")


if __name__ == "__main__":
    main()
