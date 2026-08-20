"""One-off, read-only diagnostic -- 2026-08-20. Investigates why the "NIFTY"
underlying's own WS tick stream is showing option-premium-scale values
(~120-180) instead of real index-scale values (~24000+), while
GetOptionChain's own underlying-price resolution is correct. Reuses the
already-exported Shoonya diagnostic session (read-only REST calls only,
no WS, no writes) -- see shoonya_ws_quality_diagnostic.py for the same
session-reuse pattern and why (avoids a second, possibly session-
invalidating login).

Prints every NSE search_scrip row whose tsym matches "Nifty 50" exactly
(our own matching logic in ShoonyaBrokerAdapter._resolve_underlying_token),
plus a live GetQuotes call against whatever token that resolves to, so the
real broker-side token/price can be directly compared against what's
actually flowing into price_bars/quote_ticks right now.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.broker_adapter.shoonya.rest_client import ShoonyaRestClient  # noqa: E402

SESSION_FILE = Path(__file__).resolve().parent.parent / ".ws_diagnostic_session.json"


def main() -> None:
    session = json.loads(SESSION_FILE.read_text())
    rest = ShoonyaRestClient(session["api_host"], session["access_token"])

    print("--- search_scrip('NSE', 'NIFTY') rows matching tsym == 'Nifty 50' exactly ---")
    rows = rest.search_scrip(session["uid"], "NSE", "NIFTY")
    print(f"Total rows returned: {len(rows)}")
    matches = [r for r in rows if str(r.get("tsym", "")).strip().upper() == "NIFTY 50"]
    print(f"Exact 'Nifty 50' matches: {len(matches)}")
    for row in matches:
        print("  ", row)

    if not matches:
        print("No exact match found -- can't test GetQuotes on it.")
        return

    token = str(matches[0]["token"])
    print(f"\n--- GetQuotes('NSE', token={token!r}) -- the token our matching logic would pick ---")
    quote = rest.get_quotes(session["uid"], "NSE", token)
    print("  ", quote)

    print("\n--- For comparison: all rows in the raw search, tsym + token only ---")
    for row in rows[:30]:
        print(f"  tsym={row.get('tsym')!r} token={row.get('token')!r} exch={row.get('exch')!r}")

    # 2026-08-20: identify exactly which real contract the mislabeled
    # "NIFTY" tick stream actually belongs to, by fetching the current real
    # option chain (via the real adapter -- correct anchor resolution
    # reused, not reinvented here) and finding whichever contract's LTP is
    # closest to the observed mislabeled value.
    print(
        "\n--- Real live option chain (NIFTY, 2026-08-25) -- looking for a match near 124-134 ---"
    )
    from datetime import date

    from app.config.settings import ShoonyaSettings
    from app.modules.broker_adapter.base.contracts import AuthResult
    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter

    settings = ShoonyaSettings()  # type: ignore[call-arg]
    auth_result = AuthResult(session_token=session["access_token"], account_id=session["actid"])
    adapter = ShoonyaBrokerAdapter(settings, auth_result, rest_client=rest)
    chain = adapter.get_option_chain("NIFTY", date(2026, 8, 25))
    print(f"  {len(chain.entries)} entries fetched")
    target = 128.0  # midpoint of the observed 124-134 mislabeled range
    close_matches = [
        (e.contract_symbol, e.strike, e.option_type, e.ltp)
        for e in chain.entries
        if e.ltp and abs(e.ltp - target) < 5.0
    ]
    print(f"  Contracts with LTP within 5 of {target}: {close_matches}")

    rest.close()


if __name__ == "__main__":
    main()
