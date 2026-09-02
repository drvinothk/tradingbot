"""Shared option-premium plausibility check, used by every place that
persists or reads an option contract's price: `ingestion.py`'s WS tick
handler (`_on_tick`) and REST chain-snapshot writer
(`record_option_chain_snapshot`), and `execution_engine.paper.service
.current_contract_price`'s live-tick read.

**2026-09-02 live incident**: a real NIFTY spot tick (~23,870) was returned
by the broker under an *option* contract's symbol (a token-resolution
mismatch, same root-cause class as `_MIN_PLAUSIBLE_PRICE_BY_SYMBOL`'s own
2026-08-20 incident, just leaking in the opposite direction — a real index
value landing on an option instead of an option-scale value landing on the
index). Confirmed via raw `quote_ticks`: every corrupted tick carried
`bid=0, ask=0, volume=0` alongside the implausible `ltp` — a real,
actively-quoted NIFTY/BANKNIFTY weekly option never looks like that during
market hours, which is why that's checked first and needs no tuning at all.
`current_contract_price` already had a narrower guard (`ltp >= strike`) for
its one call site, but that's asymmetric (an OTM leak like the 2026-09-02
C24000 case, spot ~23,870 < strike 24,000, slips through) and covers only
one of four read/write sites that touch an option's price — this module
exists so all of them share one definition instead of drifting independently,
which is exactly how the gap this incident exposed came to exist in the
first place.

Deliberately a single flat ceiling across both underlyings, not a per-
underlying value the way `_MIN_PLAUSIBLE_PRICE_BY_SYMBOL` is: this system
only ever trades ATM+/-3 strikes (`StrikeRankingConfig.atm_range`, never
overridden anywhere in this codebase -- confirmed by grep, not assumed).
Worked through the real worst case, 2026-09-03: NIFTY (50pt strike gap,
+/-150pt ATM window) tops out around ~900-1,050 even on a violent day
(~400-600pt intraday range) plus elevated-IV time value; BANKNIFTY (100pt
gap, +/-300pt window, larger absolute point moves) is the tighter case at
roughly ~1,800-2,000. 5000 (bumped from an initial 3000 by explicit
decision, for extra BANKNIFTY margin with no real cost) keeps a wide
multiple of headroom above either real worst case, while sitting far below
either index's own spot level (23,000+ / 51,000+) where a leaked spot tick
would land. A per-underlying ceiling would need an extra lookup (option
contract -> underlying) at the hot WS-tick path for no real precision
benefit at this margin. Same "wide floor/ceiling, not a tight band -- the
goal is catching the wrong instrument entirely, not tracking real price
movement" philosophy as that existing guard.
"""

MAX_PLAUSIBLE_OPTION_PREMIUM = 5000.0


def is_plausible_option_tick(ltp: float, bid: float, ask: float, volume: int) -> bool:
    """`False` means "don't trust this as a real option premium" -- every
    caller's response to that is the same discipline already established
    elsewhere in this codebase: drop/reject rather than persist or act on
    it, and fall through to whatever fallback chain already exists for a
    missing/stale price. Never fabricates or corrects a value.

    Two independent checks, cheapest and least tunable first:
    1. `bid == ask == volume == 0` -- a real, actively-quoted contract
       during market hours never has all three zero at once. This alone
       caught all 7 corrupted ticks in the 2026-09-02 incident.
    2. `ltp > MAX_PLAUSIBLE_OPTION_PREMIUM` -- a backstop for a corrupted
       tick that might otherwise carry a nonzero bid/ask/volume.
    """
    if bid == 0 and ask == 0 and volume == 0:
        return False
    return ltp <= MAX_PLAUSIBLE_OPTION_PREMIUM
