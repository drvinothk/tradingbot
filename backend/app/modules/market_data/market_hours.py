"""The live-market-data session schedule — pure time-boundary logic, no
threading or I/O, shared by `MarketDataScheduler` (which acts on phase
transitions) and `MarketHoursGatedProvider` (which gates outbound calls
against the current phase) so both have one source of truth instead of
each hardcoding the same three timestamps.

Deliberately broker-agnostic — this is a property of when a real market-
data session should be open at all, not anything specific to Angel One.
Applies to whichever real `BaseMarketDataProvider` is configured
(`MARKET_DATA_PROVIDER=angel_one` today, `shoonya` if ever selected as the
live-tick source again, any future real provider) uniformly, since the
gate wraps at the `BaseMarketDataProvider` composition level, not inside
any one concrete provider. See `provider_composition.get_market_data_provider`'s
own docstring for why `"mock"` is deliberately excluded from this gate.

Boundaries per 2026-08-06's explicit spec: 08:30 IST session start,
09:00 IST pre-market-to-active-market switch, 16:00 IST hard stop. NSE's
own 09:15-15:30 trading window sits inside the 09:00-16:00 "active_market"
phase deliberately — the 15 minutes on each side are for a provider's own
session warm-up/wind-down, not an attempt to model the exchange's own
hours precisely.

**2026-08-10: an optional, deliberately-bounded extension for TrueData's
aftermarket "Full Market Feed Replay" server**, which streams a prior real
trading day back as though it were live in the evening —
`Settings.market_data.is_replay_mode` (`MARKET_DATA_IS_REPLAY_MODE`, off by
default; see that field's own docstring) pushes the hard stop from 16:00 to
23:30 IST, nothing else. Deliberately *not* named to match the skeleton this
was requested from verbatim (a bare `IS_REPLAY_MODE` env var read via
`os.getenv`) — matches this codebase's own already-established
`MARKET_DATA_` prefix convention instead, the same rename
`MARKET_DATA_ALLOW_OFFHOURS_TESTING` itself went through for the identical
reason. Reuses `now_ist()`/`app.core.clock` for the IST conversion, not a
new `pytz` dependency — this module already had zero-bug IST handling
before today; the point of the 2026-08-10 Angel One fix was "use the
existing correct primitive," not "add a second one."

`current_phase`/`is_within_market_hours` both take an optional
`replay_mode` override so tests can control this deterministically without
monkeypatching settings — `None` (every real call site) means "read
`Settings.market_data.is_replay_mode`," matching the same `now: time |
None = None` pattern this module already uses for the identical reason.
Both `MarketDataScheduler` (the 16:00/23:30 hard-disconnect trigger) and
`MarketHoursGatedProvider` (the outbound-call gate) call these with no
override at all, so they automatically pick up whichever cutoff is
currently configured — one shared source of truth, unchanged from before
today, now with one more input.

**2026-09-01: `is_data_flow_expected` — a narrower predicate for "is a real
tick plausible right now", distinct from `is_within_market_hours`'s
connectivity-warm-up window.** NSE's own cash/index session genuinely opens
at 09:15 IST, a full 15 minutes after this module's own `PRE_MARKET_END`
(09:00) — deliberately, per this module's own docstring above, since
`active_market` models connectivity readiness, not the exchange's real
hours. Before this existed, every *consumer* of tick/bar freshness
(`market_data.freshness`'s age-based classification, which reads "no tick
row exists yet" as maximally-stale `DEAD` unconditionally, with zero
time-of-day awareness) had no way to distinguish "the feed is genuinely
dead" from "it's 09:05 and NSE hasn't opened yet" — confirmed live-capable
of two real, distinct bugs: a spurious `CRITICAL` `market_data_stale` alert
firing for any workspace with an ACTIVE session between ~08:35 and 09:15,
and — more seriously, since `MARKET_DATA_FAILOVER_ENABLED=true` is live in
production — `FailoverMarketDataProvider._check_primary_health` tripping to
the backup leg within `failover_threshold_seconds` (10.0s default) of a
human logging in any time before ~09:15, purely because real data hasn't
started yet, never because of an actual outage. Both are fixed by gating on
this predicate instead of the broader `is_within_market_hours()` — see
`scheduler.health_check._check_market_data_staleness` and
`market_data.providers.failover.FailoverMarketDataProvider._check_primary_
health`/`_check_recovery`.

Deliberately does **not** gate the *replay-mode* case on `NORMAL_MARKET_OPEN`
at all beyond what `current_phase` already does — TrueData's replay feed (see
the 2026-08-10 section above) only ever streams in the *evening*
(`REPLAY_MODE_MARKET_CLOSE = 23:30`), never before real market open, so the
09:15 floor is always already satisfied by the time a replay session's own
`ACTIVE_MARKET` phase begins; there is no scenario where replay data
legitimately needs to be treated as "expected" before 09:15 wall-clock IST.

**2026-09-02: an upper bound too, `DATA_FLOW_EXPECTED_END` (15:15 IST),
mirroring the 09:15 floor.** NSE's cash/index session genuinely winds down
~15:30, well before this system's own connection-lifecycle cutoff
(`MARKET_CLOSE = 16:00`); a real feed going quiet in that last stretch is
expected, not evidence of an outage, so `_check_market_data_staleness` and
`FailoverMarketDataProvider`'s health checks (the same two consumers the
09:15 floor above already protects) would otherwise spuriously alert/trip
failover for the last ~45 minutes of every trading day. Scoped to
*non-replay* mode only, same asymmetry as the floor's own replay carve-out
above but in the opposite direction: TrueData's replay feed streams from
~16:00 through `REPLAY_MODE_MARKET_CLOSE` (23:30), squarely inside this new
15:15-16:00 window, so applying the cutoff there would falsely mark every
replay tick as unexpected for the entire session.
"""

from __future__ import annotations

import enum
from datetime import time

from app.core.clock import now_ist

MARKET_OPEN = time(8, 30)
PRE_MARKET_END = time(9, 0)
MARKET_CLOSE = time(16, 0)
# See module docstring's 2026-08-10 section -- only reached when
# Settings.market_data.is_replay_mode is explicitly set.
REPLAY_MODE_MARKET_CLOSE = time(23, 30)
# NSE's own cash/index session open -- see module docstring's 2026-09-01
# section. The shared home for this constant; strategies needing it (e.g.
# ORB's opening-range anchor) should import it from here rather than
# keeping their own private copy.
NORMAL_MARKET_OPEN = time(9, 15)
# The is_data_flow_expected upper bound -- see module docstring's 2026-09-02
# section. Live (non-replay) only.
DATA_FLOW_EXPECTED_END = time(15, 15)

# This system only ever trades these two underlyings -- kept here (not
# imported from a specific broker adapter, e.g. `broker_adapter.shoonya
# .adapter.KNOWN_UNDERLYINGS`) since `MarketDataScheduler` works with
# whichever `BaseMarketDataProvider` is configured, not a specific broker's
# execution adapter -- same market-data/execution decoupling this module's
# own docstring already applies to phase timing.
TRADABLE_UNDERLYINGS: tuple[str, ...] = ("NIFTY", "BANKNIFTY")

# Symbols streamed purely for VIX/PCR environment-metrics context
# (`strategy_engine.env_metrics`), never traded and never selectable as a
# strategy's own `underlying_symbol` -- kept deliberately separate from
# TRADABLE_UNDERLYINGS above so nothing that treats that tuple as "the
# tradable universe" (strategy-underlying validation, the auto-spawner, a
# future picker UI) ever sees this symbol. Subscribed the same way
# (`MarketDataScheduler._subscribe_known_underlyings`), through the same
# provider-agnostic `ensure_ingestion_running`, just from its own loop.
ENV_METRIC_SYMBOLS: tuple[str, ...] = ("INDIA VIX",)


class MarketPhase(enum.Enum):
    PRE_MARKET = "pre_market"
    ACTIVE_MARKET = "active_market"
    CLOSED = "closed"


def _resolve_replay_mode(replay_mode: bool | None) -> bool:
    if replay_mode is None:
        # Local import: same load-time-cycle caution this codebase already
        # applies elsewhere (e.g. market_data_scheduler.ensure_market_data_
        # scheduler_running's own local get_settings import) -- this module
        # otherwise has zero app-level imports beyond app.core.clock.
        from app.config.settings import get_settings

        return get_settings().market_data.is_replay_mode
    return replay_mode


def _resolve_market_close(replay_mode: bool | None) -> time:
    resolved = _resolve_replay_mode(replay_mode)
    return REPLAY_MODE_MARKET_CLOSE if resolved else MARKET_CLOSE


def current_phase(now: time | None = None, replay_mode: bool | None = None) -> MarketPhase:
    t = now if now is not None else now_ist().time()
    market_close = _resolve_market_close(replay_mode)
    if MARKET_OPEN <= t < PRE_MARKET_END:
        return MarketPhase.PRE_MARKET
    if PRE_MARKET_END <= t < market_close:
        return MarketPhase.ACTIVE_MARKET
    return MarketPhase.CLOSED


def is_within_market_hours(now: time | None = None, replay_mode: bool | None = None) -> bool:
    return current_phase(now, replay_mode) is not MarketPhase.CLOSED


def is_data_flow_expected(now: time | None = None, replay_mode: bool | None = None) -> bool:
    """True only once a real tick is plausible -- `active_market` (09:00+)
    AND at/past NSE's own 09:15 session open, AND (live mode only) before
    15:15. A strict subset of `is_within_market_hours`'s wider 08:30-16:00
    span; see module docstring's 2026-09-01/2026-09-02 sections for why this
    exists as a second, narrower predicate rather than moving
    `PRE_MARKET_END`/`MARKET_CLOSE`/tightening `is_within_market_hours`
    itself -- connectivity readiness (warm up the WS handshake, sit idle)
    and "should absence of a tick be treated as meaningful" are genuinely
    different questions with different answers in the 09:00-09:15 and
    15:15-16:00 gaps. The 15:15 upper bound is skipped entirely in replay
    mode -- TrueData's replay feed streams from ~16:00 onward, so it would
    otherwise never be considered "expected".
    """
    t = now if now is not None else now_ist().time()
    if current_phase(t, replay_mode) is not MarketPhase.ACTIVE_MARKET or t < NORMAL_MARKET_OPEN:
        return False
    if _resolve_replay_mode(replay_mode):
        return True
    return t < DATA_FLOW_EXPECTED_END
