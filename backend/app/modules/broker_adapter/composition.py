"""The composition root every other module's docstrings already promise:
"A composition-root in `main.py` decides whether `mock` or `shoonya` gets
injected — no caller ever imports a concrete adapter directly." `get_broker`
is that decision point, lazily constructing one process-wide `BrokerPort`
instance.

This is only a meaningful pattern because `core.locking.LOCK_PROCESS_SINGLETON`
already guarantees exactly one backend process — the same reasoning
`api.v1.strategies`'s `_RUNNERS` in-memory dict already relies on for
tracking live `SyntheticStrategyRunner` threads, extended here to a second
piece of required in-process state (the paper Execution Service and every
`PositionManager` thread need to share the *same* adapter instance, since
`MockBrokerAdapter`'s internal order/position book only means anything if
there's one of it).

Phase 3 always resolves to `MockBrokerAdapter` — Phase 5 is what makes this
function branch on which broker is actually configured for the workspace.
"""

from __future__ import annotations

from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.mock.adapter import MockBrokerAdapter

_broker: BrokerPort | None = None


def get_broker() -> BrokerPort:
    global _broker
    if _broker is None:
        _broker = MockBrokerAdapter()
    return _broker


def set_broker(broker: BrokerPort | None) -> None:
    """Test/composition-root hook — lets tests inject a seeded
    `MockBrokerAdapter` (or a fake) instead of the lazily-constructed
    default, and lets `main.py` reset state between process lifespans in
    tests that exercise startup/shutdown more than once.
    """
    global _broker
    _broker = broker
