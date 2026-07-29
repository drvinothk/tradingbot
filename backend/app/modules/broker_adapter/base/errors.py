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
