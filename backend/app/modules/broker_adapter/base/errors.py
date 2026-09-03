"""Generic broker-failure exception hierarchy — every concrete adapter
(`shoonya`, any future real broker) raises these, or a subclass of these,
never a bespoke type a caller would need broker-specific knowledge to
catch. This is what lets broker-agnostic callers (`PositionManager`,
`market_data.ingestion`) react to "the broker connection is unhealthy"
without importing anything from `broker_adapter/shoonya/` — the same
boundary `composition.py`'s own docstring already establishes for
*constructing* an adapter, extended here to *failures* from one.

`MockBrokerAdapter` never raises these (it has no failure modes to speak
of), so nothing here changes behavior for Phases 1-4's tests.
"""

from __future__ import annotations


class BrokerError(Exception):
    """Base for every broker-adapter failure."""


class BrokerAuthError(BrokerError):
    """Authentication is unusable — invalid credentials, IP mismatch, TOTP
    drift, or a mid-session token that died. The only recovery is a fresh
    login; callers should treat this as "the broker connection needs a
    human," typically by moving the affected session to `degraded_mode`
    (see `PositionManager.run_once`'s own handling).
    """


class BrokerConnectivityError(BrokerError):
    """A call failed for a reason short of "credentials are dead" — a
    transient network error, an unexpected API response, a rate-limit
    timeout. Distinct from `BrokerAuthError` because the natural response
    differs (retry next cycle vs. stop and ask for re-auth).
    """


class BrokerRateLimitedError(BrokerConnectivityError):
    """The broker rejected a call specifically for exceeding a rate/quota
    limit, not for any other connectivity reason. A subclass of
    `BrokerConnectivityError` (not `BrokerAuthError` — a fresh login doesn't
    reset a rate-limit counter, so treating this like a dead credential
    would just burn another call retrying immediately) so existing
    `except BrokerConnectivityError` handling still catches it, while a
    caller that wants a longer, dedicated backoff can catch this specific
    type instead of the normal retry-next-cycle cadence. Added 2026-08-06
    after a stale Angel One token silently retried `getCandleData` every
    ~25s for ~12 hours overnight, exhausting the endpoint's rate-limit
    budget before anyone noticed — continuing to retry at the normal
    interval after that point risks prolonging the same penalty.
    """


class ConfigurationError(Exception):
    """Ops-Hardening Phase 5. Raised by `broker_adapter.composition
    .get_execution_broker` when session mode calls for real execution but
    `Settings.app.allow_real_money_dispatch` is off. Deliberately *not* a
    `BrokerError` subclass — this means "we correctly refused before ever
    touching a broker," not "the broker failed," so a caller's `except
    BrokerError` (retry-next-cycle logic) must never accidentally swallow
    it. Must propagate and halt the calling cycle, never be caught and
    downgraded to paper.
    """


class CriticalSafetyException(Exception):
    """Ops-Hardening Phase 5. For a broker adapter's own `place_order`
    pre-flight checks, when a hard safety invariant is violated before ever
    reaching the broker. Deliberately *not* a `BrokerError` subclass, same
    reasoning as `ConfigurationError` — this is "we refused to send the
    order," not "the broker rejected it." (`ShoonyaBrokerAdapter` no longer
    has an example of this — its own 1-lot hardcap was removed 2026-09-03
    once `RiskLimitConfig.per_trade_lot_cap` became a real, UI-editable
    setting rather than a fixed, non-editable adapter-level floor — but the
    type stays available for a genuine future case.)
    """
