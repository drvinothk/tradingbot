"""The abstract interface every module depends on. A composition root (wired
in app/main.py from Phase 5 onward) decides whether the `mock` or `shoonya`
implementation gets injected — no caller ever imports a concrete adapter
directly, which is what keeps "broker-agnostic" real rather than aspirational.

Synchronous by design, matching Phase 0's choice to keep the core synchronous
(see core/db/session.py) — most broker REST SDKs (including Shoonya's) are
sync-only anyway. Streaming (quotes/depth) uses a callback registered via
subscribe_quotes rather than an async generator, so the same interface shape
works whether the implementation runs its own background thread (real
WebSocket client) or just calls back synchronously from a replay loop (mock).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import date

from app.modules.broker_adapter.base.contracts import (
    AuthResult,
    DepthSnapshot,
    InstrumentInfo,
    MarginInfo,
    OptionChainSnapshot,
    OrderRequest,
    OrderResult,
    Position,
    Tick,
)

TickCallback = Callable[[Tick], None]
DepthCallback = Callable[[DepthSnapshot], None]


class BrokerPort(ABC):
    """Every method here must be implemented by both the mock adapter and
    every real broker adapter. Order-related methods exist on the interface
    now (Phase 1) even though only Execution Service calls them starting
    Phase 3 — defining the full contract once avoids a breaking interface
    change later.
    """

    @abstractmethod
    def authenticate(self) -> AuthResult: ...

    @abstractmethod
    def get_instrument_master(self, exchange: str) -> list[InstrumentInfo]:
        """Full tradable instrument list for the exchange — what the daily
        instrument/strike sync job (Scheduler) calls."""

    @abstractmethod
    def get_option_chain(self, underlying: str, expiry: date) -> OptionChainSnapshot: ...

    @abstractmethod
    def get_quote(self, contract_symbol: str) -> Tick:
        """One-shot snapshot fetch — for cases that don't need a live stream."""

    @abstractmethod
    def get_depth(self, contract_symbol: str) -> DepthSnapshot: ...

    @abstractmethod
    def subscribe_quotes(
        self,
        contract_symbols: list[str],
        on_tick: TickCallback,
        on_depth: DepthCallback | None = None,
    ) -> None:
        """Starts streaming. Real adapters run this over a single shared
        WebSocket (Shoonya only supports one connection per session — see
        Phase 0 research); the mock adapter replays recorded/synthetic data
        on a timer and invokes the same callbacks."""

    @abstractmethod
    def unsubscribe_quotes(self, contract_symbols: list[str]) -> None: ...

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResult:
        """Must be idempotent on `request.idempotency_key` — a repeated call
        with the same key returns the original result rather than submitting
        twice."""

    @abstractmethod
    def modify_order(self, broker_order_id: str, **changes: object) -> OrderResult: ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> OrderResult: ...

    @abstractmethod
    def get_order_status(self, broker_order_id: str) -> OrderResult: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_margin(self) -> MarginInfo:
        """Available funds/margin for the account — Risk Service's pre-trade
        capital check calls this instead of a fixed stub value."""
