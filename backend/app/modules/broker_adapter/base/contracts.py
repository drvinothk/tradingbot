"""Broker-agnostic DTOs. Every module in this system talks in these types,
never in a broker's native payload shape — the normalizer inside each
broker-specific adapter (e.g. modules/broker_adapter/shoonya/normalizer.py)
is the only place a raw broker response is ever touched.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime


class OptionType(enum.StrEnum):
    CE = "CE"
    PE = "PE"


class OrderSide(enum.StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(enum.StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    SL_LIMIT = "sl_limit"
    SL_MARKET = "sl_market"


class BrokerOrderStatus(enum.StrEnum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    MODIFY_PENDING = "modify_pending"
    CANCEL_PENDING = "cancel_pending"


@dataclass(frozen=True)
class InstrumentInfo:
    symbol: str
    exchange: str
    lot_size: int
    tick_size: float
    is_option: bool = False
    underlying: str | None = None
    expiry: date | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    broker_token: str = ""
    # Exchange-imposed max order size (NSE F&O "freeze quantity"), raw qty.
    # None (the default, and what every real-broker adapter produces today —
    # Shoonya's normalizer doesn't parse this field, see its own docstring)
    # means risk_engine's freeze-quantity check stays a no-op.
    freeze_qty: int | None = None


@dataclass(frozen=True)
class Tick:
    contract_symbol: str
    ltp: float
    bid: float
    ask: float
    volume: int
    oi: int | None
    ts: datetime


@dataclass(frozen=True)
class DepthLevel:
    price: float
    qty: int
    orders: int = 0


@dataclass(frozen=True)
class DepthSnapshot:
    contract_symbol: str
    bid_levels: tuple[DepthLevel, ...]
    ask_levels: tuple[DepthLevel, ...]
    ts: datetime


@dataclass(frozen=True)
class PriceCandle:
    """One completed OHLCV bar from a broker's own historical-candle
    endpoint (Shoonya: `TPSeries`), as opposed to a bar this system
    aggregates itself from a raw tick stream (see `BarAggregator`) —
    distinct enough to warrant its own DTO rather than reusing that
    module's `Bar`, since a broker-supplied candle is real, already-final
    OHLC, not something built up tick-by-tick here. `volume` is `0` for a
    symbol whose feed carries none (live-confirmed: Shoonya's NSE *index*
    tokens report zero volume on every candle, unlike a derivative
    contract's own token) — never inferred or defaulted to something else.
    """

    bucket_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class OptionChainEntry:
    contract_symbol: str
    strike: float
    option_type: OptionType
    ltp: float
    bid: float
    ask: float
    volume: int
    oi: int


@dataclass(frozen=True)
class OptionChainSnapshot:
    underlying: str
    expiry: date
    ts: datetime
    entries: tuple[OptionChainEntry, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OrderRequest:
    """`idempotency_key` is mandatory at this layer, not optional — every
    adapter implementation (mock and real) must treat a repeated key as
    "already submitted, return the prior result" rather than resubmitting.

    Ops-Hardening Phase 5: `lot_size` and `tag` are broker-agnostic (any
    future real adapter's own 1-lot hardcap needs `lot_size` the same way
    `ShoonyaBrokerAdapter` does — this isn't a Shoonya-specific field), so
    they live on this shared contract rather than being bolted onto one
    adapter. `lot_size` defaults to 1 (existing callers that never set it —
    tests constructing `OrderRequest` directly — get the strictest possible
    hardcap rather than an accidentally-permissive default). `tag` defaults
    to `""`; a real adapter appends it into whatever broker-visible
    remarks/tag field it has, alongside `idempotency_key`.
    """

    idempotency_key: str
    contract_symbol: str
    side: OrderSide
    order_type: OrderType
    qty: int
    limit_price: float | None = None
    trigger_price: float | None = None
    lot_size: int = 1
    tag: str = ""


@dataclass(frozen=True)
class OrderResult:
    idempotency_key: str
    broker_order_id: str
    status: BrokerOrderStatus
    filled_qty: int
    avg_fill_price: float | None
    raw_message: str = ""


@dataclass(frozen=True)
class Position:
    contract_symbol: str
    qty: int
    avg_price: float


@dataclass(frozen=True)
class AuthResult:
    session_token: str
    account_id: str
    expires_at: datetime | None = None


@dataclass(frozen=True)
class MarginInfo:
    available_margin: float
    used_margin: float
    total_margin: float
    ts: datetime


@dataclass(frozen=True)
class TradeFill:
    """One real, filled order for a contract, read from the broker's own
    order/trade history — not this system's own `Order` table. Built
    2026-09-02 for reconciliation's auto-repair path: when a local position
    is still OPEN but the broker shows it flat, this is how the real exit
    price/time is recovered instead of guessing or asking a human to type
    it in (see `reconciliation.service`'s own docstring for the incident
    this closes — a position squared off directly in the broker app, which
    this system's own order-lifecycle code never saw).
    """

    broker_order_id: str
    contract_symbol: str
    side: OrderSide
    qty: int
    avg_price: float
    ts: datetime
