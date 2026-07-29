"""The only place a raw Shoonya (Noren OMS) JSON payload is ever touched —
every other module in this system talks in `broker_adapter.base.contracts`
DTOs, never in Shoonya's field names. Isolating that mapping here means a
field-name mismatch discovered once real credentials exist (see this
module's own docstring caveat below) is a small, contained fix in one file,
not a redesign.

**Built from research, not a live-verified spec.** Phase 5 had no live
Shoonya account to test against — field names below come from the public
Noren-OMS API convention (consistent across every Shoonya-Dev GitHub
example and several independent broker forks reusing the same white-label
OMS, which is reasonably strong signal) plus shoonya.com's own FAQ pages.
Every parse function raises `NormalizationError` with the offending raw
payload on a missing/unexpected field, rather than a bare `KeyError`,
specifically so a real-account mismatch is immediately legible instead of
an obscure traceback three frames away from the actual bad assumption.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.modules.broker_adapter.base.contracts import (
    BrokerOrderStatus,
    DepthLevel,
    DepthSnapshot,
    InstrumentInfo,
    OptionChainEntry,
    OptionType,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    Tick,
)


class NormalizationError(ValueError):
    """A Shoonya response was missing a field this normalizer expected, or
    had a value this normalizer doesn't know how to map. Carries the raw
    payload so the caller can log it — this is exactly the "field name was
    wrong" signal Phase 5's research-only build is designed to surface
    loudly instead of silently.
    """

    def __init__(self, message: str, raw: dict | str) -> None:
        super().__init__(f"{message} (raw={raw!r})")
        self.raw = raw


def _require(raw: dict, key: str) -> object:
    if key not in raw:
        raise NormalizationError(f"missing field '{key}'", raw)
    return raw[key]


def _float(raw: dict, key: str, default: float | None = None) -> float:
    value = raw.get(key)
    if value in (None, ""):
        if default is not None:
            return default
        raise NormalizationError(f"missing/empty numeric field '{key}'", raw)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise NormalizationError(f"field '{key}' is not numeric", raw) from exc


def _int(raw: dict, key: str, default: int | None = None) -> int:
    value = raw.get(key)
    if value in (None, ""):
        if default is not None:
            return default
        raise NormalizationError(f"missing/empty integer field '{key}'", raw)
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise NormalizationError(f"field '{key}' is not an integer", raw) from exc


# -- order side / type / status ---------------------------------------------

_SIDE_TO_SHOONYA = {OrderSide.BUY: "B", OrderSide.SELL: "S"}
_SHOONYA_TO_SIDE = {v: k for k, v in _SIDE_TO_SHOONYA.items()}

# Noren's price-type codes: LMT (limit), MKT (market), SL-LMT, SL-MKT.
_ORDER_TYPE_TO_SHOONYA = {
    OrderType.LIMIT: "LMT",
    OrderType.MARKET: "MKT",
    OrderType.SL_LIMIT: "SL-LMT",
    OrderType.SL_MARKET: "SL-MKT",
}

# Noren order-book status strings, observed across the community NorenAPI
# forks researched for this phase — case as documented (upper snake-ish,
# broker-specific casing quirks are normalized via .upper() before lookup).
_STATUS_FROM_SHOONYA = {
    "PENDING": BrokerOrderStatus.PENDING,
    "OPEN": BrokerOrderStatus.OPEN,
    "TRIGGER_PENDING": BrokerOrderStatus.OPEN,
    "PARTIALLY FILLED": BrokerOrderStatus.PARTIALLY_FILLED,
    "COMPLETE": BrokerOrderStatus.FILLED,
    "CANCELED": BrokerOrderStatus.CANCELLED,
    "CANCELLED": BrokerOrderStatus.CANCELLED,
    "REJECTED": BrokerOrderStatus.REJECTED,
    "MODIFY_PENDING": BrokerOrderStatus.MODIFY_PENDING,
    "CANCEL_PENDING": BrokerOrderStatus.CANCEL_PENDING,
}


def map_order_status(raw_status: str) -> BrokerOrderStatus:
    status = _STATUS_FROM_SHOONYA.get(raw_status.strip().upper())
    if status is None:
        raise NormalizationError(f"unknown order status '{raw_status}'", raw_status)
    return status


# -- instrument master / scrip master ----------------------------------------


def parse_option_type(raw: str) -> OptionType:
    normalized = raw.strip().upper()
    if normalized in ("CE", "CALL", "C"):
        return OptionType.CE
    if normalized in ("PE", "PUT", "P"):
        return OptionType.PE
    raise NormalizationError(f"unknown option type '{raw}'", raw)


def parse_instrument_master_row(row: dict, exchange: str) -> InstrumentInfo:
    """One row of `SearchScrip`'s JSON `values` list — chosen over parsing
    Shoonya's separately-downloadable scrip-master CSV files (a bulk
    per-exchange file with its own, differently-cased column headers) so
    `get_instrument_master` has exactly one response shape to normalize,
    not two. `instname` starting with `OPT` (`OPTSTK`/`OPTIDX`) distinguishes
    an option row from an equity/index/future row — only option rows carry
    strike/expiry/option-type.
    """
    instrument_type = str(row.get("instname", "")).upper()
    is_option = instrument_type.startswith("OPT")

    symbol = str(_require(row, "tsym"))
    lot_size = _int(row, "ls")
    tick_size = _float(row, "ti")
    token = str(row.get("token", ""))

    if not is_option:
        return InstrumentInfo(
            symbol=symbol,
            exchange=exchange,
            lot_size=lot_size,
            tick_size=tick_size,
            is_option=False,
            broker_token=token,
        )

    underlying = str(row.get("symname", symbol))
    expiry_raw = str(_require(row, "exd"))
    strike = _float(row, "strprc")
    option_type_raw = str(row.get("optt", ""))

    return InstrumentInfo(
        symbol=symbol,
        exchange=exchange,
        lot_size=lot_size,
        tick_size=tick_size,
        is_option=True,
        underlying=underlying,
        expiry=parse_shoonya_date(expiry_raw),
        strike=strike,
        option_type=parse_option_type(option_type_raw),
        broker_token=token,
    )


def parse_shoonya_date(raw: str) -> date:
    """Noren dates are typically `DD-MON-YYYY` (e.g. `30-JUL-2026`) in
    scrip-master/order-book responses."""
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise NormalizationError(f"unrecognized date format '{raw}'", raw)


# -- ticks / depth -------------------------------------------------------


def parse_tick(raw: dict, contract_symbol: str) -> Tick:
    """Shared shape for both a REST `GetQuotes` response and a WebSocket
    touchline (`t: tk`/`tf`) push — Noren uses the same field names
    (`lp`=last price, `bp1`/`sp1`=best bid/ask, `v`=volume, `oi`) in both.
    """
    return Tick(
        contract_symbol=contract_symbol,
        ltp=_float(raw, "lp"),
        bid=_float(raw, "bp1", default=0.0),
        ask=_float(raw, "sp1", default=0.0),
        volume=_int(raw, "v", default=0),
        oi=_int(raw, "oi", default=0) if "oi" in raw else None,
        ts=_utcnow(),
    )


def parse_depth(raw: dict, contract_symbol: str) -> DepthSnapshot:
    """Snapquote (`t: df`/`dk`) push — 5 bid/ask levels, `bp1..bp5`/
    `bq1..bq5` (bid price/qty) and `sp1..sp5`/`sq1..sq5` (ask price/qty)."""
    bid_levels = tuple(
        DepthLevel(
            price=_float(raw, f"bp{i}", default=0.0),
            qty=_int(raw, f"bq{i}", default=0),
        )
        for i in range(1, 6)
    )
    ask_levels = tuple(
        DepthLevel(
            price=_float(raw, f"sp{i}", default=0.0),
            qty=_int(raw, f"sq{i}", default=0),
        )
        for i in range(1, 6)
    )
    return DepthSnapshot(
        contract_symbol=contract_symbol, bid_levels=bid_levels, ask_levels=ask_levels, ts=_utcnow()
    )


def parse_option_chain_entry(raw: dict, contract_symbol: str) -> OptionChainEntry:
    return OptionChainEntry(
        contract_symbol=contract_symbol,
        strike=_float(raw, "strprc", default=0.0),
        option_type=parse_option_type(str(raw.get("optt", "CE"))),
        ltp=_float(raw, "lp", default=0.0),
        bid=_float(raw, "bp1", default=0.0),
        ask=_float(raw, "sp1", default=0.0),
        volume=_int(raw, "v", default=0),
        oi=_int(raw, "oi", default=0),
    )


# -- orders / positions ----------------------------------------------------


def to_place_order_payload(
    request: OrderRequest, *, uid: str, actid: str, product_type: str = "M"
) -> dict:
    """Builds `PlaceOrder`'s jData body. `product_type` defaults to `"M"`
    (NRML/carry-forward margin product) — matches this system's intraday
    options-scalping use case better than `"I"` (MIS/intraday-only, which
    some brokers force-square-off well before this system's own EOD logic
    runs) or `"C"` (CNC, cash-and-carry, not valid for F&O). `remarks`
    carries `idempotency_key` so a duplicate submission is at least
    traceable broker-side even though the real idempotency guarantee is
    `place_order`'s own dict lookup in `ShoonyaBrokerAdapter`, not this.
    """
    return {
        "uid": uid,
        "actid": actid,
        "exch": _exchange_from_symbol(request.contract_symbol),
        "tsym": request.contract_symbol,
        "qty": str(request.qty),
        "prc": str(request.limit_price or 0),
        "trgprc": str(request.trigger_price) if request.trigger_price is not None else "0",
        "prd": product_type,
        "trantype": _SIDE_TO_SHOONYA[request.side],
        "prctyp": _ORDER_TYPE_TO_SHOONYA[request.order_type],
        "ret": "DAY",
        "remarks": request.idempotency_key,
        "ordersource": "API",
    }


def _exchange_from_symbol(contract_symbol: str) -> str:
    """Options traded via this system are always NFO (Nifty/Bank Nifty
    F&O) — matches `mock_universe.py`'s own only-ever-NFO convention.
    Kept as a function (not a hardcoded literal at each call site) so the
    one place this assumption lives is obvious if a future phase adds a
    non-NFO instrument.
    """
    return "NFO"


def parse_order_result(raw: dict, *, idempotency_key: str) -> OrderResult:
    """`PlaceOrder`'s immediate response, or one row of `OrderBook`/
    `SingleOrderHistory`. `norenordno` is Shoonya's broker-side order id;
    `s`/`stat` carries a bare `"Ok"`/`"Not_Ok"` on the immediate PlaceOrder
    ack rather than a real order status — callers needing the actual
    lifecycle status should follow up with `get_order_status` (which reads
    `OrderBook`'s per-row `status` field instead).
    """
    broker_order_id = str(raw.get("norenordno") or raw.get("nOrdNo") or "")
    if not broker_order_id:
        raise NormalizationError("missing broker order id ('norenordno'/'nOrdNo')", raw)

    status_raw = raw.get("status")
    status = map_order_status(status_raw) if status_raw else BrokerOrderStatus.PENDING

    return OrderResult(
        idempotency_key=idempotency_key,
        broker_order_id=broker_order_id,
        status=status,
        filled_qty=_int(raw, "fillshares", default=0),
        avg_fill_price=_float(raw, "avgprc", default=0.0) or None,
        raw_message=str(raw.get("emsg", raw.get("rejreason", ""))),
    )


def parse_position(raw: dict) -> Position:
    """One row of `PositionBook`. `netqty` is signed (positive = net long,
    negative = net short) — matches `Position.qty`'s own signed convention
    already used by `MockBrokerAdapter`.
    """
    return Position(
        contract_symbol=str(_require(raw, "tsym")),
        qty=_int(raw, "netqty"),
        avg_price=_float(raw, "netavgprc", default=0.0),
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)
