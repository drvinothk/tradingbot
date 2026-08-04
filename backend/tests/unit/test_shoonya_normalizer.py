from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.modules.broker_adapter.base.contracts import (
    BrokerOrderStatus,
    OptionType,
    OrderRequest,
    OrderSide,
    OrderType,
)
from app.modules.broker_adapter.shoonya import normalizer


def test_parse_instrument_master_row_underlying():
    row = {"tsym": "NIFTY", "ls": "25", "ti": "0.05", "token": "26000", "instname": "IDX"}
    info = normalizer.parse_instrument_master_row(row, "NFO")
    assert info.symbol == "NIFTY"
    assert info.is_option is False
    assert info.lot_size == 25
    assert info.broker_token == "26000"


def test_parse_instrument_master_row_option():
    row = {
        "tsym": "NIFTY30JUL2624000CE",
        "ls": "25",
        "ti": "0.05",
        "token": "12345",
        "instname": "OPTIDX",
        "symname": "NIFTY",
        "exd": "30-JUL-2026",
        "strprc": "24000",
        "optt": "CE",
    }
    info = normalizer.parse_instrument_master_row(row, "NFO")
    assert info.is_option is True
    assert info.underlying == "NIFTY"
    assert info.expiry == date(2026, 7, 30)
    assert info.strike == 24000.0
    assert info.option_type == OptionType.CE


def test_parse_instrument_master_row_missing_field_raises():
    with pytest.raises(normalizer.NormalizationError):
        normalizer.parse_instrument_master_row({"ls": "25", "ti": "0.05"}, "NFO")


def test_parse_instrument_master_row_falls_back_to_tsym_when_strprc_missing():
    """Live-found: a real NFO SearchScrip response had `strprc` missing from
    every weekly-tagged NIFTY option row (41/41 for that day's chain), while
    monthly rows carried it normally. Strike is still recoverable from the
    trading symbol's own fixed suffix.
    """
    row = {
        "tsym": "NIFTY04AUG26C18500",
        "ls": "65",
        "ti": "0.05",
        "token": "48399",
        "instname": "OPTIDX",
        "symname": "NIFTY",
        "exd": "04-AUG-2026",
        "weekly": "W1",
        "optt": "CE",
        # no strprc — the real, live-observed shape
    }
    info = normalizer.parse_instrument_master_row(row, "NFO")
    assert info.strike == 18500.0


def test_parse_instrument_master_row_raises_when_strprc_missing_and_tsym_unparseable():
    row = {
        "tsym": "SOMETHING-WEIRD",
        "ls": "65",
        "ti": "0.05",
        "token": "1",
        "instname": "OPTIDX",
        "symname": "NIFTY",
        "exd": "04-AUG-2026",
        "optt": "CE",
    }
    with pytest.raises(normalizer.NormalizationError):
        normalizer.parse_instrument_master_row(row, "NFO")


def test_parse_option_chain_entry_falls_back_to_tsym_when_strprc_missing():
    """Pre-emptive hardening: `GetOptionChain` is a different endpoint from
    `SearchScrip`, but if it turns out to share the same real, live-observed
    gap for weekly contracts, a defaulted `0.0` strike would silently
    corrupt ranking for a whole expiry's worth of strikes rather than
    report one honestly.
    """
    row = {"token": "48399", "optt": "CE", "instname": "OPTIDX"}  # no strprc
    entry = normalizer.parse_option_chain_entry(row, "NIFTY04AUG26C18500")
    assert entry.strike == 18500.0


def test_parse_option_chain_entry_zero_strike_when_tsym_also_unparseable():
    row = {"token": "1", "optt": "CE", "instname": "OPTIDX"}  # no strprc
    entry = normalizer.parse_option_chain_entry(row, "SOMETHING-WEIRD")
    assert entry.strike == 0.0


@pytest.mark.parametrize("raw,expected", [("CE", OptionType.CE), ("PE", OptionType.PE)])
def test_parse_option_type(raw, expected):
    assert normalizer.parse_option_type(raw) == expected


def test_parse_option_type_unknown_raises():
    with pytest.raises(normalizer.NormalizationError):
        normalizer.parse_option_type("XX")


def test_parse_tick():
    raw = {"lp": "123.45", "bp1": "123.0", "sp1": "124.0", "v": "5000", "oi": "20000"}
    tick = normalizer.parse_tick(raw, "NIFTY30JUL2624000CE")
    assert tick.contract_symbol == "NIFTY30JUL2624000CE"
    assert tick.ltp == 123.45
    assert tick.bid == 123.0
    assert tick.ask == 124.0
    assert tick.volume == 5000
    assert tick.oi == 20000


def test_parse_tick_missing_ltp_raises():
    with pytest.raises(normalizer.NormalizationError):
        normalizer.parse_tick({"bp1": "1"}, "SYM")


def test_parse_depth():
    raw = {f"bp{i}": str(100 - i) for i in range(1, 6)}
    raw.update({f"bq{i}": str(i * 10) for i in range(1, 6)})
    raw.update({f"sp{i}": str(100 + i) for i in range(1, 6)})
    raw.update({f"sq{i}": str(i * 20) for i in range(1, 6)})
    depth = normalizer.parse_depth(raw, "SYM")
    assert len(depth.bid_levels) == 5
    assert len(depth.ask_levels) == 5
    assert depth.bid_levels[0].price == 99.0
    assert depth.ask_levels[0].price == 101.0


@pytest.mark.parametrize(
    "raw_status,expected",
    [
        ("COMPLETE", BrokerOrderStatus.FILLED),
        ("open", BrokerOrderStatus.OPEN),
        ("REJECTED", BrokerOrderStatus.REJECTED),
        ("CANCELED", BrokerOrderStatus.CANCELLED),
        ("TRIGGER_PENDING", BrokerOrderStatus.OPEN),
    ],
)
def test_map_order_status(raw_status, expected):
    assert normalizer.map_order_status(raw_status) == expected


def test_map_order_status_unknown_raises():
    with pytest.raises(normalizer.NormalizationError):
        normalizer.map_order_status("SOME_NEW_STATUS")


def test_to_place_order_payload_shape():
    request = OrderRequest(
        idempotency_key="key-1",
        contract_symbol="NIFTY30JUL2624000CE",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=25,
        limit_price=120.5,
    )
    payload = normalizer.to_place_order_payload(request, uid="FA1", actid="FA1")
    assert payload["uid"] == "FA1"
    assert payload["exch"] == "NFO"
    assert payload["tsym"] == "NIFTY30JUL2624000CE"
    assert payload["trantype"] == "B"
    assert payload["prctyp"] == "LMT"
    assert payload["remarks"] == "key-1"


def test_parse_order_result_requires_broker_order_id():
    with pytest.raises(normalizer.NormalizationError):
        normalizer.parse_order_result({"status": "COMPLETE"}, idempotency_key="k")


def test_parse_order_result_defaults_to_pending_without_status():
    result = normalizer.parse_order_result({"norenordno": "123"}, idempotency_key="k")
    assert result.status == BrokerOrderStatus.PENDING
    assert result.broker_order_id == "123"


def test_parse_position_signed_qty():
    position = normalizer.parse_position(
        {"tsym": "NIFTY30JUL2624000CE", "netqty": "-25", "netavgprc": "119.5"}
    )
    assert position.qty == -25
    assert position.avg_price == 119.5


def test_parse_tpseries_row():
    """Live-confirmed shape against a real account: `TPSeries` returns real
    OHLC for NSE index tokens (contrary to a documented, unresolved
    community report that index historical queries return nothing on
    Shoonya) — `ssboe` (epoch seconds) is used for `bucket_start`, not the
    also-present `time` string, since it needs no locale/format guessing.
    """
    row = {
        "stat": "Ok",
        "time": "04-08-2026 13:55:00",
        "ssboe": "1785831900",
        "into": "24446.40",
        "inth": "24448.40",
        "intl": "24444.90",
        "intc": "24446.80",
        "intvwap": "24446.80",
        "intv": "0",
        "intoi": "0",
    }
    candle = normalizer.parse_tpseries_row(row)
    assert candle.bucket_start == datetime.fromtimestamp(1785831900, tz=UTC)
    assert candle.open == 24446.40
    assert candle.high == 24448.40
    assert candle.low == 24444.90
    assert candle.close == 24446.80
    assert candle.volume == 0


def test_parse_tpseries_row_real_volume_on_a_derivative_token():
    """Same endpoint, a futures token instead of an index token — live-
    confirmed real, non-zero volume, unlike the index case above. Not a
    parsing difference, a genuine broker-side data difference between the
    two token types.
    """
    row = {
        "stat": "Ok",
        "time": "04-08-2026 13:55:00",
        "ssboe": "1785831900",
        "into": "24510.00",
        "inth": "24519.00",
        "intl": "24505.10",
        "intc": "24510.10",
        "intvwap": "24511.19",
        "intv": "8970",
        "intoi": "1365",
    }
    candle = normalizer.parse_tpseries_row(row)
    assert candle.volume == 8970
