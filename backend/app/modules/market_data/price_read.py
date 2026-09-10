"""Provenance-tagged option-contract price read — what the execution-path
price resolver (`execution_engine.paper.service.current_contract_price`)
returns instead of a bare `Tick`, so every caller makes **one** explicit
decision from `(source, freshness, plausible)` rather than re-deriving
"is this safe to act on?" ad hoc at each call site.

Introduced 2026-09-11 after the QC of the 2026-09-10 spot-leak batch
(`cf50b80`): `current_contract_price`'s fallback chain had grown three
inconsistently-gated rungs (the live-feed rung freshness+plausibility gated,
the chain-snapshot rung gated by neither, the last-resort rung returning a
known-implausible `broker.get_quote()` value) and callers could not tell
which rung a price came from — `PositionManager` got a bolt-on
`is_plausible_option_tick` re-check, `eod_square_off` got nothing. See
docs/ops/shoonya_option_chain_spot_leak.md (Rail 6).

`Tick` itself is deliberately left unchanged — it is a frozen dataclass
shared with the broker layer, and adding a `source` field there would ripple
into every adapter. Provenance lives in this wrapper instead.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.modules.broker_adapter.base.contracts import Tick
from app.modules.market_data.freshness import FreshnessState

# LIVE and DEGRADED are the two states a poll-driven consumer may act on
# (matches `fresh_tick_or_none` / `fresh_reference_premium`'s own bar). STALE
# and DEAD are returned *labelled* so a forced-close path (eod_square_off) can
# still use the number knowingly while `PositionManager._run_cycle` skips it.
_ACTIONABLE_FRESHNESS = (FreshnessState.LIVE, FreshnessState.DEGRADED)


class PriceSource(enum.StrEnum):
    """Where a `PriceRead`'s price actually came from — the diagnostic every
    consumer keys its decision on."""

    # An independent market-data provider's live WS tick (the active feed, or
    # an EXECUTION_PRICE_SECONDARY_FEED leg), fresh and plausible.
    LIVE_FEED = "live_feed"
    # The REST `OptionChainSnapshot` — the same source the strategy proposed
    # the trade from. `freshness` says how old; may be DEGRADED or STALE.
    CHAIN_SNAPSHOT = "chain_snapshot"
    # Last-resort `broker.get_quote()` — the mock's synthetic for a paper
    # position, or Shoonya's own (spot-leak-prone) GetQuotes for a live one.
    BROKER_QUOTE = "broker_quote"
    # Nothing usable anywhere. `tick` is None. The caller MUST NOT act on a
    # number — never a fabricated or spot-shaped value.
    NONE = "none"


@dataclass(frozen=True)
class PriceRead:
    tick: Tick | None
    source: PriceSource
    freshness: FreshnessState
    plausible: bool

    @classmethod
    def none(cls) -> PriceRead:
        return cls(
            tick=None,
            source=PriceSource.NONE,
            freshness=FreshnessState.DEAD,
            plausible=False,
        )

    @property
    def is_actionable(self) -> bool:
        """Safe to feed to `evaluate_open_position` / archive as this
        contract's LTP: a plausible tick that is LIVE or DEGRADED. A STALE
        (or DEAD) snapshot, an implausible read, or `NONE` is not.
        """
        return (
            self.tick is not None
            and self.plausible
            and self.freshness in _ACTIONABLE_FRESHNESS
        )

    @property
    def ltp_or_none(self) -> float | None:
        return self.tick.ltp if self.tick is not None else None
