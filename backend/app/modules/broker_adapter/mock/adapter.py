"""Mock/replay broker adapter — a synthetic random-walk price generator
implementing the full BrokerPort interface. This is what every Phase 1-4
module is built and tested against; the real Shoonya adapter (Phase 5) slots
in behind the same interface with zero changes required upstream.

Seedable for deterministic tests (`MockBrokerAdapter(seed=...)` reproduces
the exact same tick sequence), unseeded for interactive dev exploration.
"""

from __future__ import annotations

import random
import threading
import time
import uuid
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.modules.broker_adapter.base.broker_port import BrokerPort, DepthCallback, TickCallback
from app.modules.broker_adapter.base.contracts import (
    AuthResult,
    BrokerOrderStatus,
    DepthLevel,
    DepthSnapshot,
    InstrumentInfo,
    MarginInfo,
    OptionChainEntry,
    OptionChainSnapshot,
    OptionType,
    OrderRequest,
    OrderResult,
    OrderSide,
    Position,
    Tick,
)
from app.modules.broker_adapter.base.errors import BrokerConnectivityError

# Generous synthetic funds — paper mode should never be capital-constrained by
# a fake margin figure; this exists purely so the DTO is populated, not to
# model real margin math (see MTF_STUB_LEVERAGE_FACTOR's own docstring in
# risk_engine/service.py for the equivalent reasoning on the leverage side).
_SYNTHETIC_TOTAL_MARGIN = 10_000_000.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class FillScenario:
    """One queued `place_order` outcome, consumed FIFO per `contract_symbol`
    by `queue_fill_scenario`/`place_order` below — opt-in fault injection
    for partial fills, rejects, and delays. `filled_qty=None` defaults to
    the full requested quantity; `avg_fill_price=None` defaults to the
    normal computed price when `status` is FILLED, or to no price at all
    otherwise (a REJECTED/PENDING order realistically has none) — this is
    what lets a test exercise `close_position`'s handling of a non-terminal
    exit order without contriving a fill price for it.
    """

    status: BrokerOrderStatus
    filled_qty: int | None = None
    avg_fill_price: float | None = None
    delay_seconds: float = 0.0


class MockBrokerAdapter(BrokerPort):
    def __init__(
        self,
        instruments: list[InstrumentInfo] | None = None,
        seed: int | None = None,
        tick_interval_seconds: float = 1.0,
    ) -> None:
        self._instruments = instruments or []
        self._rng = random.Random(seed)
        self._tick_interval_seconds = tick_interval_seconds

        # contract_symbol -> synthetic mid price, seeded on first touch
        self._prices: dict[str, float] = {}

        self._stream_thread: threading.Thread | None = None
        self._stream_stop = threading.Event()
        self._subscribed: set[str] = set()
        self._on_tick: TickCallback | None = None
        self._on_depth: DepthCallback | None = None

        # idempotency_key -> OrderResult, and a running position book
        self._orders: dict[str, OrderResult] = {}
        self._positions: dict[str, Position] = {}

        # Opt-in fault injection — both empty/False by default, meaning
        # every existing caller's behavior is unchanged unless a test
        # explicitly queues a scenario or simulates a disconnect.
        self._fill_scenarios: dict[str, list[FillScenario]] = {}
        self._disconnected = False

    def queue_fill_scenario(self, contract_symbol: str, scenario: FillScenario) -> None:
        """The next `place_order` call for this symbol consumes one queued
        scenario instead of the adapter's normal always-fills-immediately
        behavior. Queue is per-symbol and FIFO; an unqueued symbol behaves
        exactly as before this existed.
        """
        self._fill_scenarios.setdefault(contract_symbol, []).append(scenario)

    def simulate_disconnect(self, disconnected: bool = True) -> None:
        """While `True`, `get_quote`/`place_order` raise
        `BrokerConnectivityError` instead of their normal behavior —
        standing in for a dropped broker connection. Off by default.
        """
        self._disconnected = disconnected

    # -- price simulation -------------------------------------------------

    def _price_for(self, contract_symbol: str) -> float:
        if contract_symbol not in self._prices:
            # Deterministic seed price by symbol so re-subscribing mid-run
            # doesn't jump: hash the symbol into a plausible option-premium
            # range. Uses zlib.crc32, not Python's built-in hash() — str
            # hashing is salted per-process (PYTHONHASHSEED) by default, so
            # hash() would silently break the "same seed -> same prices"
            # guarantee across separate runs, only appearing to work within
            # a single process (which is exactly how the bug hid during
            # testing: two adapters in the *same* test process agreed).
            base = 50.0 + (zlib.crc32(contract_symbol.encode("utf-8")) % 200)
            self._prices[contract_symbol] = float(base)
        return self._prices[contract_symbol]

    def _step_price(self, contract_symbol: str) -> float:
        current = self._price_for(contract_symbol)
        # Random walk, bounded away from zero — option premiums don't go negative.
        pct_move = self._rng.gauss(0, 0.004)
        new_price = max(0.5, current * (1 + pct_move))
        self._prices[contract_symbol] = new_price
        return new_price

    def _make_tick(self, contract_symbol: str, *, step: bool) -> Tick:
        price = self._step_price(contract_symbol) if step else self._price_for(contract_symbol)
        spread = max(0.05, price * 0.001)
        return Tick(
            contract_symbol=contract_symbol,
            ltp=round(price, 2),
            bid=round(price - spread, 2),
            ask=round(price + spread, 2),
            volume=self._rng.randint(100, 5000),
            oi=self._rng.randint(1000, 50000),
            ts=_utcnow(),
        )

    def _make_depth(self, contract_symbol: str) -> DepthSnapshot:
        price = self._price_for(contract_symbol)
        tick_size = 0.05
        bid_levels = tuple(
            DepthLevel(
                price=round(price - tick_size * (i + 1), 2),
                qty=self._rng.randint(50, 2000),
                orders=self._rng.randint(1, 20),
            )
            for i in range(5)
        )
        ask_levels = tuple(
            DepthLevel(
                price=round(price + tick_size * (i + 1), 2),
                qty=self._rng.randint(50, 2000),
                orders=self._rng.randint(1, 20),
            )
            for i in range(5)
        )
        return DepthSnapshot(
            contract_symbol=contract_symbol,
            bid_levels=bid_levels,
            ask_levels=ask_levels,
            ts=_utcnow(),
        )

    # -- BrokerPort: session -----------------------------------------------

    def authenticate(self) -> AuthResult:
        return AuthResult(
            session_token=f"mock-token-{uuid.uuid4().hex[:12]}", account_id="MOCK-ACCT"
        )

    # -- BrokerPort: instrument / chain data -------------------------------

    def get_instrument_master(self, exchange: str) -> list[InstrumentInfo]:
        return [i for i in self._instruments if i.exchange == exchange]

    def get_option_chain(self, underlying: str, expiry: date) -> OptionChainSnapshot:
        contracts = [
            i
            for i in self._instruments
            if i.is_option and i.underlying == underlying and i.expiry == expiry
        ]
        entries = tuple(
            OptionChainEntry(
                contract_symbol=c.symbol,
                strike=c.strike or 0.0,
                option_type=c.option_type or OptionType.CE,
                ltp=(tick := self._make_tick(c.symbol, step=False)).ltp,
                bid=tick.bid,
                ask=tick.ask,
                volume=tick.volume,
                oi=tick.oi or 0,
            )
            for c in contracts
        )
        return OptionChainSnapshot(
            underlying=underlying, expiry=expiry, ts=_utcnow(), entries=entries
        )

    # -- BrokerPort: quotes / depth ------------------------------------------

    def get_quote(self, contract_symbol: str) -> Tick:
        if self._disconnected:
            raise BrokerConnectivityError("mock broker: simulated disconnect")
        return self._make_tick(contract_symbol, step=False)

    def get_depth(self, contract_symbol: str) -> DepthSnapshot:
        return self._make_depth(contract_symbol)

    def subscribe_quotes(
        self,
        contract_symbols: list[str],
        on_tick: TickCallback,
        on_depth: DepthCallback | None = None,
    ) -> None:
        self._subscribed.update(contract_symbols)
        self._on_tick = on_tick
        self._on_depth = on_depth

        if self._stream_thread is None or not self._stream_thread.is_alive():
            self._stream_stop.clear()
            self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True)
            self._stream_thread.start()

    def unsubscribe_quotes(self, contract_symbols: list[str]) -> None:
        """When this empties `_subscribed`, joins the stream thread before
        returning rather than just signaling it to stop — a caller that
        tears down its DB/session right after `unsubscribe_quotes()`
        returns (every test using a real `session_factory` does exactly
        this) must not race an in-flight `_stream_loop` iteration's
        `on_tick` callback, which can still be mid-flight past the stop
        event (it only checks `_stream_stop` between iterations, via
        `.wait(...)`, not inside one). Found live: a `QuoteTick` insert
        landing after the test's own cleanup had already deleted
        `QuoteTick` rows, leaving one FK'd to an `Instrument` the same
        cleanup then failed to delete.
        """
        self._subscribed.difference_update(contract_symbols)
        if not self._subscribed:
            self._stream_stop.set()
            if self._stream_thread is not None:
                self._stream_thread.join(timeout=5.0)

    def _stream_loop(self) -> None:
        while not self._stream_stop.is_set():
            for symbol in list(self._subscribed):
                tick = self._make_tick(symbol, step=True)
                if self._on_tick is not None:
                    self._on_tick(tick)
                if self._on_depth is not None and self._rng.random() < 0.3:
                    self._on_depth(self._make_depth(symbol))
            self._stream_stop.wait(self._tick_interval_seconds)

    # -- BrokerPort: orders -------------------------------------------------

    def _apply_position_fill(self, request: OrderRequest, fill_price: float) -> None:
        signed_qty = request.qty if request.side == OrderSide.BUY else -request.qty
        existing = self._positions.get(request.contract_symbol)
        if existing is None:
            self._positions[request.contract_symbol] = Position(
                contract_symbol=request.contract_symbol, qty=signed_qty, avg_price=fill_price
            )
        else:
            new_qty = existing.qty + signed_qty
            self._positions[request.contract_symbol] = Position(
                contract_symbol=request.contract_symbol, qty=new_qty, avg_price=fill_price
            )

    def place_order(self, request: OrderRequest) -> OrderResult:
        if request.idempotency_key in self._orders:
            return self._orders[request.idempotency_key]

        if self._disconnected:
            raise BrokerConnectivityError("mock broker: simulated disconnect")

        queued = self._fill_scenarios.get(request.contract_symbol)
        scenario = queued.pop(0) if queued else None

        if scenario is not None:
            if scenario.delay_seconds:
                time.sleep(scenario.delay_seconds)
            if scenario.avg_fill_price is not None:
                fill_price: float | None = scenario.avg_fill_price
            elif scenario.status == BrokerOrderStatus.FILLED:
                fill_price = request.limit_price or self._price_for(request.contract_symbol)
            else:
                fill_price = None
            filled_qty = scenario.filled_qty if scenario.filled_qty is not None else request.qty

            result = OrderResult(
                idempotency_key=request.idempotency_key,
                broker_order_id=f"MOCK-{uuid.uuid4().hex[:10]}",
                status=scenario.status,
                filled_qty=filled_qty,
                avg_fill_price=round(fill_price, 2) if fill_price is not None else None,
            )
            self._orders[request.idempotency_key] = result
            if scenario.status == BrokerOrderStatus.FILLED and fill_price is not None:
                self._apply_position_fill(request, fill_price)
            return result

        fill_price = request.limit_price or self._price_for(request.contract_symbol)
        result = OrderResult(
            idempotency_key=request.idempotency_key,
            broker_order_id=f"MOCK-{uuid.uuid4().hex[:10]}",
            status=BrokerOrderStatus.FILLED,
            filled_qty=request.qty,
            avg_fill_price=round(fill_price, 2),
        )
        self._orders[request.idempotency_key] = result
        self._apply_position_fill(request, fill_price)
        return result

    def modify_order(self, broker_order_id: str, **changes: object) -> OrderResult:
        for result in self._orders.values():
            if result.broker_order_id == broker_order_id:
                return result
        raise KeyError(f"Unknown mock order: {broker_order_id}")

    def cancel_order(self, broker_order_id: str) -> OrderResult:
        for key, result in self._orders.items():
            if result.broker_order_id == broker_order_id:
                cancelled = OrderResult(
                    idempotency_key=result.idempotency_key,
                    broker_order_id=result.broker_order_id,
                    status=BrokerOrderStatus.CANCELLED,
                    filled_qty=result.filled_qty,
                    avg_fill_price=result.avg_fill_price,
                )
                self._orders[key] = cancelled
                return cancelled
        raise KeyError(f"Unknown mock order: {broker_order_id}")

    def get_order_status(self, broker_order_id: str) -> OrderResult:
        return self.modify_order(broker_order_id)

    def get_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.qty != 0]

    def get_margin(self) -> MarginInfo:
        used = sum(abs(p.qty) * p.avg_price for p in self._positions.values() if p.qty != 0)
        return MarginInfo(
            available_margin=_SYNTHETIC_TOTAL_MARGIN - used,
            used_margin=used,
            total_margin=_SYNTHETIC_TOTAL_MARGIN,
            ts=_utcnow(),
        )
