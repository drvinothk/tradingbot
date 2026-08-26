"""Ops-Hardening Phase 5. Pre-flight checks run immediately before any
*real* `place_order` call — separate from `broker_adapter.composition`
(which decides *whether* a real broker should be used at all) and from
Risk Service's own `evaluate_trade_intent` (which already ran a richer
budget-vs-committed-capital check earlier in the pipeline). This is the
last gate, right at the point of actually touching a real broker, for
staleness that could only have developed *since* that earlier check —
most concretely, `approve_trade_approval`'s human-approval path can call
`dispatch_trade_intent` minutes after the original signal, well past
whatever was fresh at evaluate() time.

Every check raises on failure — never returns a boolean for a caller to
maybe-check. `dispatch_trade_intent` runs this inside its own
`LOCK_EXECUTION_SINGLETON` scope, before `place_order`; a raise there
propagates out uncaught, which is the explicit design intent: register the
order attempt as failed and halt the calling strategy cycle, never
silently retry as paper.

2026-08-26: the option-chain freshness check that used to live here moved
to the caller (`execution_engine.paper.service`) — this module's own
docstring (and `market_data.freshness`'s own) both claim `broker_adapter`/
`market_data` depend on each other in one direction only; this was the one
import making that bidirectional. Broker connectivity + margin are the
genuinely broker_adapter-scoped checks that belong here.
"""

from __future__ import annotations

from app.domain.session.models import TradingSession
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.errors import BrokerError, ConfigurationError
from app.modules.broker_adapter.composition import is_execution_broker_connected


def run_preflight_checks(
    broker: BrokerPort,
    *,
    trading_session: TradingSession,
) -> None:
    if not is_execution_broker_connected():
        raise ConfigurationError(
            f"Pre-flight failed for trading_session {trading_session.id}: Shoonya is not "
            "connected -- refusing real dispatch."
        )

    try:
        margin = broker.get_margin()
    except BrokerError as exc:
        raise ConfigurationError(
            f"Pre-flight failed for trading_session {trading_session.id}: could not "
            f"confirm margin ({exc}) -- refusing real dispatch."
        ) from exc
    if margin.available_margin <= 0:
        raise ConfigurationError(
            f"Pre-flight failed for trading_session {trading_session.id}: no available "
            f"margin ({margin.available_margin}) -- refusing real dispatch."
        )
