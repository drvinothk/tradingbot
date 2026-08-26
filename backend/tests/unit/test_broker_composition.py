from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from app.config.settings import get_settings
from app.domain.session.models import SafeMode, TradingSession
from app.modules.broker_adapter import composition
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.contracts import (
    AuthResult,
    DepthSnapshot,
    InstrumentInfo,
    MarginInfo,
    OptionChainSnapshot,
    OrderRequest,
    OrderResult,
    Position,
    PriceCandle,
    Tick,
)
from app.modules.broker_adapter.base.errors import BrokerAuthError, ConfigurationError
from app.modules.broker_adapter.mock.adapter import MockBrokerAdapter


class _FakeRealBroker(BrokerPort):
    """Stands in for `ShoonyaBrokerAdapter` — tracks whether `close()` was
    called and lets a test make any given method raise `BrokerAuthError` on
    demand, without needing a real Shoonya account.
    """

    def __init__(self) -> None:
        self.closed = False
        self.margin_raises: BrokerAuthError | None = None

    def authenticate(self) -> AuthResult:
        return AuthResult(session_token="tok", account_id="acc")

    def get_instrument_master(self, exchange: str) -> list[InstrumentInfo]:
        return []

    def get_option_chain(self, underlying: str, expiry: date) -> OptionChainSnapshot:
        return OptionChainSnapshot(underlying=underlying, expiry=expiry, ts=datetime.now(UTC))

    def get_price_history(
        self, underlying: str, start: datetime, end: datetime, timeframe_seconds: int = 60
    ) -> list[PriceCandle]:
        return []

    def get_quote(self, contract_symbol: str) -> Tick:
        return Tick(contract_symbol, 0.0, 0.0, 0.0, 0, None, datetime.now(UTC))

    def get_depth(self, contract_symbol: str) -> DepthSnapshot:
        return DepthSnapshot(contract_symbol, (), (), datetime.now(UTC))

    def subscribe_quotes(self, contract_symbols, on_tick, on_depth=None) -> None:
        return None

    def unsubscribe_quotes(self, contract_symbols) -> None:
        return None

    def place_order(self, request: OrderRequest) -> OrderResult:
        raise NotImplementedError

    def modify_order(self, broker_order_id: str, **changes: object) -> OrderResult:
        raise NotImplementedError

    def cancel_order(self, broker_order_id: str) -> OrderResult:
        raise NotImplementedError

    def get_order_status(self, broker_order_id: str) -> OrderResult:
        raise NotImplementedError

    def get_positions(self) -> list[Position]:
        return []

    def get_margin(self) -> MarginInfo:
        if self.margin_raises is not None:
            raise self.margin_raises
        return MarginInfo(0.0, 0.0, 0.0, datetime.now(UTC))

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset():
    composition.reset_for_tests()
    yield
    composition.reset_for_tests()


def test_set_broker_marks_connected_and_status_reflects_it():
    fake = _FakeRealBroker()
    composition.set_broker(fake)
    assert composition.is_shoonya_configured() is True


def test_set_broker_none_disconnects():
    composition.set_broker(_FakeRealBroker())
    composition.set_broker(None)
    assert composition.is_shoonya_configured() is False


def test_set_broker_closes_previous_real_adapter_on_swap():
    first = _FakeRealBroker()
    composition.set_broker(first)
    composition.set_broker(_FakeRealBroker())
    assert first.closed is True


def test_set_broker_none_closes_the_installed_adapter():
    fake = _FakeRealBroker()
    composition.set_broker(fake)
    composition.set_broker(None)
    assert fake.closed is True


def test_set_broker_over_execution_mock_default_does_not_crash():
    """`get_broker()`'s lazy default (the persistent execution `MockBrokerAdapter`)
    has no `close()` at all — swapping it out for a real broker must not
    blow up on the duck-typed close check.
    """
    composition.get_broker()  # populates _broker with the execution mock default
    composition.set_broker(_FakeRealBroker())
    assert composition.is_shoonya_configured() is True


def test_broker_auth_error_from_any_call_site_marks_disconnected():
    fake = _FakeRealBroker()
    fake.margin_raises = BrokerAuthError("session expired")
    composition.set_broker(fake)
    assert composition.is_shoonya_configured() is True

    broker = composition.get_broker()
    with pytest.raises(BrokerAuthError):
        broker.get_margin()

    assert composition.is_shoonya_configured() is False


def test_broker_auth_error_still_propagates_to_the_caller():
    fake = _FakeRealBroker()
    fake.margin_raises = BrokerAuthError("session expired")
    composition.set_broker(fake)
    broker = composition.get_broker()

    with pytest.raises(BrokerAuthError, match="session expired"):
        broker.get_margin()


def test_non_auth_calls_are_unaffected_when_connected():
    fake = _FakeRealBroker()
    composition.set_broker(fake)
    broker = composition.get_broker()
    assert broker.get_positions() == []
    assert composition.is_shoonya_configured() is True


# -- Ops-Hardening Phase 5: get_execution_broker gating ----------------------


def _session(mode: SafeMode) -> TradingSession:
    return TradingSession(id=uuid.uuid4(), mode=mode)


def _allow_real_money(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(get_settings().app, "allow_real_money_dispatch", value)


@pytest.mark.parametrize(
    "mode",
    [
        SafeMode.PAPER_ONLY,
        SafeMode.DEGRADED_MODE,
        SafeMode.KILL_SWITCH,
        SafeMode.RECONCILIATION_LOCK,
    ],
)
def test_protective_modes_always_return_mock_even_with_flag_on(monkeypatch, mode):
    _allow_real_money(monkeypatch, True)
    composition.set_broker(_FakeRealBroker())  # a real broker IS connected

    broker = composition.get_execution_broker(_session(mode))

    assert isinstance(broker, MockBrokerAdapter)


def test_live_enabled_without_flag_raises(monkeypatch):
    _allow_real_money(monkeypatch, False)
    composition.set_broker(_FakeRealBroker())

    with pytest.raises(ConfigurationError, match="ALLOW_REAL_MONEY_DISPATCH"):
        composition.get_execution_broker(_session(SafeMode.LIVE_ENABLED))


def test_live_enabled_with_flag_but_no_connected_broker_raises(monkeypatch):
    _allow_real_money(monkeypatch, True)
    # No set_broker call -- is_shoonya_configured() is False.

    with pytest.raises(ConfigurationError, match="no real Shoonya broker"):
        composition.get_execution_broker(_session(SafeMode.LIVE_ENABLED))


def test_live_enabled_with_flag_and_connected_broker_returns_real(monkeypatch):
    # set_broker wraps the real adapter in _AuthAwareBroker, so this checks
    # "not the mock" + is_execution_broker_live, not object identity.
    _allow_real_money(monkeypatch, True)
    composition.set_broker(_FakeRealBroker())

    broker = composition.get_execution_broker(_session(SafeMode.LIVE_ENABLED))

    assert not isinstance(broker, MockBrokerAdapter)
    assert composition.is_execution_broker_live(broker) is True


def test_auth_aware_broker_overrides_every_broker_port_method():
    """A future `BrokerPort` method added as a *concrete* (non-abstract)
    method would silently resolve to `BrokerPort`'s own default instead of
    proxying to `_inner` -- with no error, unlike an abstract method, which
    Python's `ABCMeta` already guarantees is overridden (else
    `_AuthAwareBroker` couldn't be instantiated at all). Comparing method
    *names* directly (not `__abstractmethods__`) catches that gap
    regardless of whether the new method is abstract or concrete.
    """
    port_methods = {
        name
        for name, member in vars(BrokerPort).items()
        if callable(member) and not name.startswith("_")
    }
    assert port_methods, "sanity check: BrokerPort should expose public methods"

    missing = port_methods - set(vars(composition._AuthAwareBroker))
    assert missing == set(), f"_AuthAwareBroker does not override: {sorted(missing)}"


def test_unwrap_broker_returns_the_real_adapter_underneath_auth_aware_broker(monkeypatch):
    real = _FakeRealBroker()
    composition.set_broker(real)

    wrapped = composition.get_broker()

    assert wrapped is not real  # confirms it really is wrapped
    assert composition.unwrap_broker(wrapped) is real


def test_unwrap_broker_returns_the_broker_itself_when_not_wrapped():
    mock = MockBrokerAdapter()

    assert composition.unwrap_broker(mock) is mock


def test_guarded_live_with_no_strategy_run_returns_mock_even_with_flag_on(monkeypatch):
    _allow_real_money(monkeypatch, True)
    composition.set_broker(_FakeRealBroker())

    broker = composition.get_execution_broker(_session(SafeMode.PAPER_PLUS_GUARDED_LIVE))

    assert isinstance(broker, MockBrokerAdapter)


def test_is_execution_broker_live_distinguishes_mock_from_real():
    assert (
        composition.is_execution_broker_live(composition.get_execution_mock()) is False
    )
    assert composition.is_execution_broker_live(_FakeRealBroker()) is True
