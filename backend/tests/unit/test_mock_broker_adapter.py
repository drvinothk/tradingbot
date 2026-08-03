from __future__ import annotations

import time
from datetime import date

import pytest

from app.modules.broker_adapter.base import (
    BrokerOrderStatus,
    InstrumentInfo,
    OptionType,
    OrderRequest,
    OrderSide,
    OrderType,
)
from app.modules.broker_adapter.mock import MockBrokerAdapter

EXPIRY = date(2026, 7, 30)


def _instruments() -> list[InstrumentInfo]:
    return [
        InstrumentInfo(
            symbol="NIFTY30JUL26C24000",
            exchange="NFO",
            lot_size=25,
            tick_size=0.05,
            is_option=True,
            underlying="NIFTY",
            expiry=EXPIRY,
            strike=24000,
            option_type=OptionType.CE,
        ),
        InstrumentInfo(
            symbol="NIFTY30JUL26P24000",
            exchange="NFO",
            lot_size=25,
            tick_size=0.05,
            is_option=True,
            underlying="NIFTY",
            expiry=EXPIRY,
            strike=24000,
            option_type=OptionType.PE,
        ),
        InstrumentInfo(symbol="RELIANCE-EQ", exchange="NSE", lot_size=1, tick_size=0.05),
    ]


def test_seeded_adapter_is_deterministic():
    # `ts` is real wall-clock time by design (not seeded) — compare only the
    # RNG-derived fields, which is the actual determinism guarantee here.
    a = MockBrokerAdapter(instruments=_instruments(), seed=7)
    b = MockBrokerAdapter(instruments=_instruments(), seed=7)
    tick_a = a.get_quote("NIFTY30JUL26C24000")
    tick_b = b.get_quote("NIFTY30JUL26C24000")
    assert (tick_a.ltp, tick_a.bid, tick_a.ask, tick_a.volume, tick_a.oi) == (
        tick_b.ltp,
        tick_b.bid,
        tick_b.ask,
        tick_b.volume,
        tick_b.oi,
    )


def test_instrument_master_filters_by_exchange():
    api = MockBrokerAdapter(instruments=_instruments(), seed=1)
    nfo = api.get_instrument_master("NFO")
    assert {i.symbol for i in nfo} == {"NIFTY30JUL26C24000", "NIFTY30JUL26P24000"}
    assert api.get_instrument_master("NSE")[0].symbol == "RELIANCE-EQ"


def test_option_chain_returns_only_matching_underlying_and_expiry():
    api = MockBrokerAdapter(instruments=_instruments(), seed=1)
    chain = api.get_option_chain("NIFTY", EXPIRY)
    assert len(chain.entries) == 2
    assert {e.option_type for e in chain.entries} == {OptionType.CE, OptionType.PE}


def test_prices_never_go_negative_or_zero():
    api = MockBrokerAdapter(instruments=_instruments(), seed=3)
    for _ in range(500):
        tick = api._make_tick("NIFTY30JUL26C24000", step=True)  # noqa: SLF001
        assert tick.ltp > 0
        assert tick.bid > 0


def test_subscribe_quotes_streams_ticks():
    api = MockBrokerAdapter(instruments=_instruments(), seed=5, tick_interval_seconds=0.1)
    received = []
    api.subscribe_quotes(["NIFTY30JUL26C24000"], on_tick=received.append)
    time.sleep(0.55)
    api.unsubscribe_quotes(["NIFTY30JUL26C24000"])
    count_after_stop = len(received)
    time.sleep(0.3)
    assert len(received) >= 3
    assert len(received) == count_after_stop, "ticks kept arriving after unsubscribe"


def test_place_order_is_idempotent():
    api = MockBrokerAdapter(instruments=_instruments(), seed=9)
    request = OrderRequest(
        idempotency_key="intent-1",
        contract_symbol="NIFTY30JUL26C24000",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=25,
    )
    first = api.place_order(request)
    second = api.place_order(request)
    assert first == second
    assert first.status == BrokerOrderStatus.FILLED


def test_positions_reflect_buy_and_sell():
    api = MockBrokerAdapter(instruments=_instruments(), seed=11)
    buy = OrderRequest(
        idempotency_key="buy-1",
        contract_symbol="NIFTY30JUL26C24000",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=25,
    )
    sell = OrderRequest(
        idempotency_key="sell-1",
        contract_symbol="NIFTY30JUL26C24000",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        qty=10,
    )
    api.place_order(buy)
    api.place_order(sell)
    positions = api.get_positions()
    assert len(positions) == 1
    assert positions[0].qty == 15


def test_cancel_order_updates_status():
    api = MockBrokerAdapter(instruments=_instruments(), seed=13)
    request = OrderRequest(
        idempotency_key="intent-cancel",
        contract_symbol="NIFTY30JUL26C24000",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=25,
        limit_price=100.0,
    )
    placed = api.place_order(request)
    cancelled = api.cancel_order(placed.broker_order_id)
    assert cancelled.status == BrokerOrderStatus.CANCELLED


def test_unknown_order_id_raises():
    api = MockBrokerAdapter(instruments=_instruments(), seed=17)
    with pytest.raises(KeyError):
        api.get_order_status("does-not-exist")


def test_get_margin_reflects_open_positions():
    api = MockBrokerAdapter(instruments=_instruments(), seed=19)
    baseline = api.get_margin()
    assert baseline.used_margin == 0.0
    assert baseline.available_margin == baseline.total_margin

    api.place_order(
        OrderRequest(
            idempotency_key="margin-buy-1",
            contract_symbol="NIFTY30JUL26C24000",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=25,
            limit_price=100.0,
        )
    )
    after_buy = api.get_margin()
    assert after_buy.used_margin == pytest.approx(25 * 100.0)
    expected_available = after_buy.total_margin - after_buy.used_margin
    assert after_buy.available_margin == pytest.approx(expected_available)
