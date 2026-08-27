from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta

import pytest

from app.modules.broker_adapter.base.contracts import (
    BrokerOrderStatus,
    InstrumentInfo,
    OptionType,
    OrderRequest,
    OrderSide,
    OrderType,
)
from app.modules.broker_adapter.base.errors import BrokerConnectivityError
from app.modules.broker_adapter.mock import MockBrokerAdapter
from app.modules.broker_adapter.mock.adapter import FillScenario

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


def test_get_price_history_returns_one_candle_per_bucket():
    api = MockBrokerAdapter(instruments=_instruments(), seed=1)
    start = datetime(2026, 8, 4, 9, 15, tzinfo=UTC)
    end = datetime(2026, 8, 4, 9, 20, tzinfo=UTC)

    candles = api.get_price_history("NIFTY", start, end, timeframe_seconds=60)

    assert len(candles) == 5
    buckets = [c.bucket_start for c in candles]
    assert buckets == sorted(buckets)
    assert buckets[1] - buckets[0] == timedelta(seconds=60)


def test_get_price_history_is_deterministic_across_calls():
    """A real broker's history endpoint gives the same answer for the same
    window every time it's asked — this must too, unlike `_step_price`'s
    live stateful walk that streaming subscribers consume.
    """
    api = MockBrokerAdapter(instruments=_instruments(), seed=1)
    start = datetime(2026, 8, 4, 9, 15, tzinfo=UTC)
    end = datetime(2026, 8, 4, 9, 20, tzinfo=UTC)

    first = api.get_price_history("NIFTY", start, end, timeframe_seconds=60)
    second = api.get_price_history("NIFTY", start, end, timeframe_seconds=60)

    assert first == second


def test_get_price_history_does_not_disturb_live_streaming_price():
    """Calling the history endpoint must not consume `_step_price`'s live
    random walk — otherwise a strategy polling history while another part
    of the system streams live ticks for the same symbol would see the live
    stream jump/skip whenever history happened to be queried.
    """
    api = MockBrokerAdapter(instruments=_instruments(), seed=1)
    live_before = api._price_for("NIFTY")  # noqa: SLF001

    api.get_price_history(
        "NIFTY",
        datetime(2026, 8, 4, 9, 15, tzinfo=UTC),
        datetime(2026, 8, 4, 10, 15, tzinfo=UTC),
        timeframe_seconds=60,
    )

    assert api._price_for("NIFTY") == live_before  # noqa: SLF001


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


def _order(idempotency_key: str, qty: int = 25) -> OrderRequest:
    return OrderRequest(
        idempotency_key=idempotency_key,
        contract_symbol="NIFTY30JUL26C24000",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=qty,
    )


def test_place_order_without_a_queued_scenario_is_unchanged():
    """Default-path regression check — the whole point of Batch 3's fault
    injection being opt-in: nothing queued means byte-identical behavior to
    before FillScenario existed.
    """
    api = MockBrokerAdapter(instruments=_instruments(), seed=21)
    result = api.place_order(_order("default-1"))
    assert result.status == BrokerOrderStatus.FILLED
    assert result.filled_qty == 25
    assert result.avg_fill_price is not None


def test_queued_partial_fill_scenario_is_consumed_once():
    api = MockBrokerAdapter(instruments=_instruments(), seed=23)
    api.queue_fill_scenario(
        "NIFTY30JUL26C24000",
        FillScenario(status=BrokerOrderStatus.PARTIALLY_FILLED, filled_qty=10),
    )

    partial = api.place_order(_order("partial-1"))
    assert partial.status == BrokerOrderStatus.PARTIALLY_FILLED
    assert partial.filled_qty == 10

    # Queue is drained — the next call for the same symbol falls back to
    # normal (unqueued) behavior.
    normal = api.place_order(_order("partial-2"))
    assert normal.status == BrokerOrderStatus.FILLED
    assert normal.filled_qty == 25


def test_queued_reject_scenario_has_no_fill_price_and_no_position():
    api = MockBrokerAdapter(instruments=_instruments(), seed=29)
    api.queue_fill_scenario("NIFTY30JUL26C24000", FillScenario(status=BrokerOrderStatus.REJECTED))

    result = api.place_order(_order("reject-1"))

    assert result.status == BrokerOrderStatus.REJECTED
    assert result.avg_fill_price is None
    assert api.get_positions() == []


def test_queued_scenario_can_override_fill_price_explicitly():
    api = MockBrokerAdapter(instruments=_instruments(), seed=31)
    api.queue_fill_scenario(
        "NIFTY30JUL26C24000",
        FillScenario(status=BrokerOrderStatus.FILLED, avg_fill_price=123.45),
    )

    result = api.place_order(_order("explicit-price-1"))

    assert result.avg_fill_price == 123.45


def test_simulate_disconnect_raises_on_get_quote_and_place_order():
    api = MockBrokerAdapter(instruments=_instruments(), seed=37)
    api.simulate_disconnect(True)

    with pytest.raises(BrokerConnectivityError):
        api.get_quote("NIFTY30JUL26C24000")
    with pytest.raises(BrokerConnectivityError):
        api.place_order(_order("disconnected-1"))

    api.simulate_disconnect(False)
    assert api.place_order(_order("disconnected-2")).status == BrokerOrderStatus.FILLED


def test_seed_position_restores_a_signed_position_into_the_book():
    api = MockBrokerAdapter(instruments=_instruments(), seed=41)

    api.seed_position("NIFTY30JUL26C24000", 50, 101.5)
    api.seed_position("NIFTY30JUL26P24000", -75, 88.0)

    by_symbol = {p.contract_symbol: p for p in api.get_positions()}
    assert by_symbol["NIFTY30JUL26C24000"].qty == 50
    assert by_symbol["NIFTY30JUL26P24000"].qty == -75
    assert by_symbol["NIFTY30JUL26C24000"].avg_price == 101.5


def test_seed_position_zero_qty_clears_the_entry():
    api = MockBrokerAdapter(instruments=_instruments(), seed=43)
    api.seed_position("NIFTY30JUL26C24000", 25, 100.0)
    assert api.get_positions()

    api.seed_position("NIFTY30JUL26C24000", 0, 0.0)
    assert api.get_positions() == []


def test_seed_position_then_close_via_place_order_nets_to_flat():
    api = MockBrokerAdapter(instruments=_instruments(), seed=47)
    api.seed_position("NIFTY30JUL26C24000", 25, 100.0)

    api.place_order(
        OrderRequest(
            idempotency_key="close-seeded-1",
            contract_symbol="NIFTY30JUL26C24000",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            qty=25,
        )
    )

    assert api.get_positions() == []
