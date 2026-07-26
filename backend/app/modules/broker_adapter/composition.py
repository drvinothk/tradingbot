"""The composition root every other module's docstrings already promise:
"A composition-root in `main.py` decides whether `mock` or `shoonya` gets
injected — no caller ever imports a concrete adapter directly." `get_broker`
is that decision point, lazily constructing one process-wide `BrokerPort`
instance.

This is only a meaningful pattern because `core.locking.LOCK_PROCESS_SINGLETON`
already guarantees exactly one backend process — the same reasoning
`api.v1.strategies`'s `_RUNNERS` in-memory dict already relies on for
tracking live `StrategyRunner` threads, extended here to a second
piece of required in-process state (the paper Execution Service and every
`PositionManager` thread need to share the *same* adapter instance, since
`MockBrokerAdapter`'s internal order/position book only means anything if
there's one of it).

Phase 3 always resolves to `MockBrokerAdapter` — Phase 5 is what makes this
function branch on which broker is actually configured for the workspace.

The lazy default is seeded with a real synthetic instrument universe
(`build_mock_universe`) — found missing during Phase 4's manual QC:
`get_option_chain()` against a bare `MockBrokerAdapter()` always returned
empty (ticks/orders/positions all work regardless, hash-seeded by symbol
string, independent of `self._instruments`), so no strategy could ever rank a
real contract against the live singleton, only against tests' own explicitly
seeded adapters. `app.main`'s startup also calls `sync_instrument_master`
against this same instance so `instruments`/`option_contracts` DB rows match
what it actually quotes.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.domain.market.mock_universe import build_mock_universe
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.mock.adapter import MockBrokerAdapter

_broker: BrokerPort | None = None


def _next_weekly_expiry() -> date:
    """Nearest Thursday on/after today — matches real NIFTY/BANKNIFTY weekly
    expiry cadence closely enough for a synthetic universe; exact day doesn't
    matter since nothing here is priced against a real listing."""
    today = date.today()
    return today + timedelta(days=(3 - today.weekday()) % 7)


def get_broker() -> BrokerPort:
    global _broker
    if _broker is None:
        _broker = MockBrokerAdapter(instruments=build_mock_universe(_next_weekly_expiry()))
    return _broker


def set_broker(broker: BrokerPort | None) -> None:
    """Test/composition-root hook — lets tests inject a seeded
    `MockBrokerAdapter` (or a fake) instead of the lazily-constructed
    default, and lets `main.py` reset state between process lifespans in
    tests that exercise startup/shutdown more than once.
    """
    global _broker
    _broker = broker
