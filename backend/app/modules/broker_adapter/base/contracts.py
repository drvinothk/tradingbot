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
    """

    idempotency_key: str
    contract_symbol: str
    side: OrderSide
    order_type: OrderType
    qty: int
    limit_price: float | None = None
    trigger_price: float | None = None


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
