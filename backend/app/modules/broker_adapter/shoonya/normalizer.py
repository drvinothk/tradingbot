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

**2026-08-12: Shoonya's real option trading-symbol format is confirmed --
`DDMMMYY` + `C`/`P` + strike (e.g. `NIFTY18AUG26C24400`), never a `CE`/`PE`
suffix (`NIFTY18AUG2624400CE`).** This module's own `_TSYM_STRIKE_SUFFIX`
regex and `_strike_from_tsym`'s docstring already had this right, built
from a real, live-observed row (`NIFTY04AUG26C18500`) months ago -- but a
*different*, unverified `CE`/`PE`-suffix assumption had separately crept
into this Shoonya module's own test fixtures and a one-off data-correction
script, and was never actually tested against a real broker call until it
got live-rejected: `GetOptionChain: HTTP 400 {"stat":"Not_Ok","emsg":
"Invalid Input : BANKNIFTY25AUG2657900PE is Invalid Trading Symbol."}`.
Root cause: that wrong convention traces back to the same stale/non-existent
`2026-08-13` expiry data this whole system spent two days treating as
correct (see the build plan's "Known open items" for the full incident) --
once that data's origin is suspect, so is anything modeled after it,
including symbol format. The fix was two one-off DB corrections (not a
code bug in this file, which already parsed correctly), plus updating this
module's own previously-misleading test fixtures to match. **This is
Shoonya-specific** -- Angel One, TrueData, and the mock adapter each have
their own, unrelated, already-correct symbol conventions; nothing about
this finding applies to them, and nothing in their own modules should be
changed on the strength of it. `to_place_order_payload` below was never
actually a construction bug either -- it always passed `request.
contract_symbol` through verbatim; the wrong format only ever entered via
data that was wrong before it reached this module.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from app.core.db.base import utcnow as _utcnow
from app.modules.broker_adapter.base.contracts import (
    BrokerOrderStatus,
    DepthLevel,
    DepthSnapshot,
    InstrumentInfo,
    MarginInfo,
    OptionChainEntry,
    OptionType,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    PriceCandle,
    Tick,
    TradeFill,
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


_TSYM_STRIKE_SUFFIX = re.compile(r"\d{2}[A-Z]{3}\d{2}[CP](\d+(?:\.\d+)?)$")


def _strike_from_tsym(tsym: str) -> float | None:
    """Fallback for a real, live-observed Shoonya data gap: every weekly
    (`weekly` field present) NIFTY option row from a real `SearchScrip` NFO
    search came back with no `strprc` field at all, while every monthly row
    carried one normally — not a single bad row, 41/41 of that day's weekly
    chain. The strike is still recoverable without guessing: Noren tsyms end
    in a fixed `DDMMMYY` + `C`/`P` + strike suffix (e.g.
    `NIFTY04AUG26C18500` -> strike `18500`), true regardless of what the
    leading underlying symbol is.
    """
    match = _TSYM_STRIKE_SUFFIX.search(tsym)
    if match is None:
        return None
    return float(match.group(1))


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
    strike_raw = row.get("strprc")
    if strike_raw in (None, ""):
        strike = _strike_from_tsym(symbol)
        if strike is None:
            raise NormalizationError(
                f"missing 'strprc' and could not derive strike from tsym {symbol!r}", row
            )
    else:
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


def parse_option_chain_entry(
    raw: dict, contract_symbol: str, quote: dict | None = None
) -> OptionChainEntry:
    """`raw` is one `GetOptionChain` row — live-confirmed to carry only
    structural contract data (`strprc`/`optt`/`token`/`tsym`/...), never
    quote fields (`lp`/`bp1`/`sp1`/`v`/`oi`) despite `normalizer.parse_tick`
    reading those exact field names elsewhere — `GetOptionChain` and
    `GetQuotes` are genuinely different response shapes, not the same shape
    with sometimes-missing fields. `quote` is a separate, per-contract
    `GetQuotes` response the caller fetches and passes in; defaults to `{}`
    (all-zero entry) when the caller couldn't get one, rather than pretending
    `raw` ever had that data. `strike` prefers `strprc` but falls back to
    `_strike_from_tsym` when absent — pre-emptive hardening against the same
    real, live-observed gap `parse_instrument_master_row` hit for every
    weekly NIFTY *SearchScrip* row (missing `strprc` entirely); a defaulted
    `0.0` strike would silently corrupt ranking for a whole expiry's worth
    of strikes rather than report one honestly.
    """
    quote = quote or {}
    strike_raw = raw.get("strprc")
    strike = (
        _float(raw, "strprc")
        if strike_raw not in (None, "")
        else (_strike_from_tsym(contract_symbol) or 0.0)
    )
    return OptionChainEntry(
        contract_symbol=contract_symbol,
        strike=strike,
        option_type=parse_option_type(str(raw.get("optt", "CE"))),
        ltp=_float(quote, "lp", default=0.0),
        bid=_float(quote, "bp1", default=0.0),
        ask=_float(quote, "sp1", default=0.0),
        volume=_int(quote, "v", default=0),
        oi=_int(quote, "oi", default=0),
    )


# -- historical candles ------------------------------------------------------


def parse_tpseries_row(raw: dict) -> PriceCandle:
    """One row of `TPSeries` — `ssboe` (epoch seconds) is used for
    `bucket_start` rather than the also-present `time` string, since it
    needs no locale/format guessing (unlike `exd`'s `DD-MON-YYYY`
    elsewhere in this module, `ssboe` is already an unambiguous number).
    `intv` (volume) is read with a `0` default, not required — live-
    confirmed real for Shoonya's NSE index tokens (NIFTY/BANKNIFTY spot),
    which report zero volume on every candle while a derivative contract's
    own token reports real volume on the same call.
    """
    return PriceCandle(
        bucket_start=datetime.fromtimestamp(_int(raw, "ssboe"), tz=UTC),
        open=_float(raw, "into"),
        high=_float(raw, "inth"),
        low=_float(raw, "intl"),
        close=_float(raw, "intc"),
        volume=_int(raw, "intv", default=0),
    )


# -- orders / positions ----------------------------------------------------


def to_place_order_payload(
    request: OrderRequest, *, uid: str, actid: str, product_type: str = "M"
) -> dict:
    """Builds `PlaceOrder`'s jData body. `product_type` defaults to `"M"`
    (NRML/carry-forward margin product) — matches this system's intraday
    options-scalping use case better than `"I"` (MIS/intraday-only, which
    some brokers force-square-off well before this system's own EOD logic
    runs) or `"C"` (CNC, cash-and-carry, not valid for F&O).

    `remarks` is `f"{idempotency_key}|{tag}"` (or bare `idempotency_key`
    when `tag` is empty, the existing format, byte-for-byte, for every
    caller that never sets `tag`) — `idempotency_key` always comes first
    and is never truncated/altered, since
    `ShoonyaBrokerAdapter._find_order_by_remarks`'s crash-recovery lookup
    depends on being able to find it as a substring of whatever's on the
    broker's own order-book row (see that method's own docstring) — a
    plain equality check would have broken the moment `tag` started
    getting appended here.
    """
    remarks = f"{request.idempotency_key}|{request.tag}" if request.tag else request.idempotency_key
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
        "remarks": remarks,
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
    """`PlaceOrder`'s immediate response, one row of `OrderBook`/
    `SingleOrderHistory`, or a WS order-update push -- all three carry the
    order id as `norenordno` (`nOrdNo` in some SDKs' own field-name
    convention). `s`/`stat` carries a bare `"Ok"`/`"Not_Ok"` on the
    immediate PlaceOrder ack rather than a real order status — callers
    needing the actual lifecycle status should follow up with
    `get_order_status` (which reads `OrderBook`'s per-row `status` field
    instead).

    **`ModifyOrder`/`CancelOrder` real bug, live-confirmed 2026-08-25 on
    this system's first-ever real SL/TSL test**: those two endpoints'
    responses are a genuinely different, narrower shape --
    `{"stat": "Ok", "result": "<broker_order_id>"}`, no `status`/
    `fillshares`/`avgprc` at all (Noren's ack model: a `stat: Ok` here only
    means "the modify/cancel *request* was accepted for processing," not
    that it has actually taken effect yet -- the real outcome only arrives
    later via a WS order-update push, exactly the two-step flow this
    codebase's own `sync_resting_protective_stop`/
    `cancel_resting_protective_stop` already correctly wait for). Neither
    call had ever run against a real account before that session (see
    `ShoonyaBrokerAdapter.modify_order`'s own docstring: "Phase B found zero
    production callers"), so this response shape was untested, not just
    unimplemented. `result` is accepted here as a third broker-order-id key
    for exactly that shape -- safe to add unconditionally since Shoonya's
    `PlaceOrder`/`OrderBook`/`SingleOrdHist`/WS-push shapes never define an
    unrelated `result` field of their own to collide with. Deliberately
    NOT trying to also infer a real `status` from this ack alone (still
    defaults to `PENDING` below, same as before) -- `stat: Ok` genuinely
    isn't "cancelled"/"modified" yet, and guessing otherwise would be
    exactly the premature inference `cancel_resting_protective_stop`'s own
    "never guess" contract exists to avoid.
    """
    broker_order_id = str(raw.get("norenordno") or raw.get("nOrdNo") or raw.get("result") or "")
    if not broker_order_id:
        raise NormalizationError(
            "missing broker order id ('norenordno'/'nOrdNo'/'result')", raw
        )

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


def parse_trade_fill(raw: dict) -> TradeFill:
    """One `OrderBook` row already confirmed `status == "COMPLETE"` by the
    caller (`ShoonyaBrokerAdapter.get_recent_trades`) — this function
    doesn't re-check status, only extracts the fill itself. `trantype`
    reuses the same `B`/`S` convention `to_place_order_payload` writes.

    **Fill timestamp is best-effort, unconfirmed against a real account**
    (this codebase has no live-captured `OrderBook` row to confirm Noren's
    exact field name from, unlike `avgprc`/`fillshares`/`norenordno`, which
    every other function in this module already relies on with real
    evidence) — tries the field names documented across community Noren-OMS
    forks (`norentm`, `exch_tm`, `flltm`) and falls back to "now" rather
    than raising, since `get_recent_trades`'s only caller (reconciliation's
    auto-repair path) needs the real *price* above all -- an approximate
    timestamp on an already-rare repair path is a far smaller risk than
    failing the whole repair over a field name that may not even be the one
    Shoonya actually uses.
    """
    ts_raw = raw.get("norentm") or raw.get("exch_tm") or raw.get("flltm")
    ts = _utcnow()
    if ts_raw:
        for fmt in ("%d-%m-%Y %H:%M:%S", "%H:%M:%S %d-%m-%Y"):
            try:
                ts = datetime.strptime(str(ts_raw), fmt).replace(tzinfo=UTC)
                break
            except ValueError:
                continue

    return TradeFill(
        broker_order_id=str(raw.get("norenordno") or raw.get("nOrdNo") or ""),
        contract_symbol=str(_require(raw, "tsym")),
        side=_SHOONYA_TO_SIDE.get(str(raw.get("trantype", "")).strip().upper(), OrderSide.BUY),
        qty=_int(raw, "fillshares", default=0),
        avg_price=_float(raw, "avgprc", default=0.0),
        ts=ts,
    )


def parse_margin(raw: dict) -> MarginInfo:
    """`Limits` response. **Live-corrected 2026-08-18**: a real captured
    response (`{'cash': '253.58', 'payin': '26000.00', 'payout': '0.00',
    'blk_amt': '0.00', 'mr_der_a': '18377.51', ...}`) confirmed two things
    the prior, research-only version got wrong. First, `marginused` — the
    field this function used to read as `used` — does not exist anywhere in
    a real response; it silently defaulted to 0.0 every time, which
    happened to be harmless only because `used` was never the actual
    problem. Second, and the real bug: `cash` alone is *not* total
    available funds — a same-day deposit lands in `payin`, not `cash`,
    until settlement (a well-documented Noren-OMS-family convention, not
    unique to Shoonya) — so a user who deposits real money mid-session saw
    it silently ignored, reproducing the exact `margin_check_failed`
    rejection depositing was meant to fix. `total` now folds in `payin`/
    `payout`; `used` reads `blk_amt` (amount currently blocked/reserved —
    the only field in the real response that plausibly maps to "unavailable
    right now", `blk_amt=0.00` in the captured no-open-position case, so
    this couldn't be cross-checked against a nonzero real value yet).
    `mr_der_a` ("margin required — derivatives, actual"?) looks like it may
    also be a used-margin candidate but its exact semantics are still
    unconfirmed — revisit if `blk_amt` and `mr_der_a` ever diverge in a way
    that makes one of them clearly wrong. Defaults to 0.0 rather than
    raising on a missing field (unlike `parse_tick`'s required `lp`): a
    margin figure that's merely conservative-wrong (reads as less available
    than reality) is a safer failure mode here than a hard crash on every
    pre-trade check once this is wired into Risk Service.
    """
    cash = _float(raw, "cash", default=0.0)
    payin = _float(raw, "payin", default=0.0)
    payout = _float(raw, "payout", default=0.0)
    used = _float(raw, "blk_amt", default=0.0)
    total = cash + payin - payout
    return MarginInfo(
        available_margin=total - used, used_margin=used, total_margin=total, ts=_utcnow()
    )
