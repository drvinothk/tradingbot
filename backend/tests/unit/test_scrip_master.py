"""Pure-function tests for the Angel One scrip-master parser and the
in-memory symbol/token mapper — no DB (see test_scrip_master_sync.py for the
DB-backed upsert path). Row fixtures use the exact shape from the
user-supplied Angel One doc extraction:
`{"token": "58784", "symbol": "NIFTY28OCT2524400CE", "name": "NIFTY",
"expiry": "28OCT2025", "strike": "2440000.000000", "lotsize": "75",
"instrumenttype": "OPTIDX", "exch_seg": "NFO", "tick_size": "5.000000"}`.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.market.models import OptionType
from app.modules.market_data.scrip_master import (
    ScripMasterParseError,
    ScripMasterService,
    parse_angel_expiry,
    parse_scrip_row,
)

NIFTY_CE_ROW = {
    "token": "58784",
    "symbol": "NIFTY28OCT2524400CE",
    "name": "NIFTY",
    "expiry": "28OCT2025",
    "strike": "2440000.000000",
    "lotsize": "75",
    "instrumenttype": "OPTIDX",
    "exch_seg": "NFO",
    "tick_size": "5.000000",
}


def test_strike_price_is_divided_by_100():
    """The confirmed quirk: Angel's own `strike` field is the real strike
    price multiplied by 100, as a string — `"2440000.000000"` really means
    24400.0, not 2,440,000.
    """
    row = parse_scrip_row(NIFTY_CE_ROW)
    assert row is not None
    assert row.strike == 24400.0


def test_expiry_ddmmmyyyy_format_is_parsed():
    assert parse_angel_expiry("28OCT2025") == date(2025, 10, 28)


def test_expiry_parse_error_on_garbage():
    with pytest.raises(ScripMasterParseError):
        parse_angel_expiry("not-a-date")


def test_option_type_derived_from_symbol_suffix():
    ce_row = parse_scrip_row(NIFTY_CE_ROW)
    pe_row = parse_scrip_row({**NIFTY_CE_ROW, "symbol": "NIFTY28OCT2524400PE", "token": "58785"})
    assert ce_row is not None and ce_row.option_type == OptionType.CE
    assert pe_row is not None and pe_row.option_type == OptionType.PE


def test_row_with_neither_ce_nor_pe_suffix_is_skipped():
    bad = {**NIFTY_CE_ROW, "symbol": "NIFTY28OCT2524400XX"}
    assert parse_scrip_row(bad) is None


def test_untracked_underlying_is_skipped():
    row = {**NIFTY_CE_ROW, "name": "RELIANCE"}
    assert parse_scrip_row(row) is None


def test_bse_or_other_segment_is_skipped():
    row = {**NIFTY_CE_ROW, "exch_seg": "BSE"}
    assert parse_scrip_row(row) is None


def test_futures_row_is_indexed_with_no_strike_or_option_type():
    row = {
        "token": "12345",
        "symbol": "NIFTY28OCT25FUT",
        "name": "NIFTY",
        "expiry": "28OCT2025",
        "strike": "-1.000000",  # Angel uses -1 for a non-option instrument
        "lotsize": "75",
        "instrumenttype": "FUTIDX",
        "exch_seg": "NFO",
        "tick_size": "0.05",
    }
    parsed = parse_scrip_row(row)
    assert parsed is not None
    assert parsed.kind == "future"
    assert parsed.strike is None
    assert parsed.option_type is None


def test_index_row_on_nse_segment_with_no_strike_is_recognized():
    row = {
        "token": "99926000",
        "symbol": "Nifty 50",
        "name": "NIFTY",
        "expiry": "",
        "strike": "-1.000000",
        "lotsize": "1",
        "instrumenttype": "",
        "exch_seg": "NSE",
        "tick_size": "0.05",
    }
    parsed = parse_scrip_row(row)
    assert parsed is not None
    assert parsed.kind == "index"
    assert parsed.expiry is None
    assert parsed.strike is None


def test_malformed_row_is_skipped_not_fatal():
    """One bad row must never abort a whole file's parsing — same discipline
    as ShoonyaBrokerAdapter.get_instrument_master's per-row try/except.
    """
    missing_token = {k: v for k, v in NIFTY_CE_ROW.items() if k != "token"}
    assert parse_scrip_row(missing_token) is None


def test_fetch_and_parse_filters_and_indexes(monkeypatch):
    service = ScripMasterService()
    rows = [
        NIFTY_CE_ROW,
        {**NIFTY_CE_ROW, "name": "RELIANCE", "token": "1"},  # dropped: untracked underlying
        {**NIFTY_CE_ROW, "symbol": "NIFTY28OCT2524400PE", "token": "58785"},
    ]
    monkeypatch.setattr(service, "_download", lambda: rows)

    count = service.fetch_and_parse()

    assert count == 2  # the RELIANCE row is filtered out
    assert service._rows_by_key[  # noqa: SLF001
        ("NIFTY", "option", date(2025, 10, 28), 24400.0, OptionType.CE)
    ].token == "58784"


def test_get_shoonya_tsym_falls_back_to_symbol_unchanged_when_unmapped():
    """Our own DB symbol already *is* the Shoonya tsym today — an unmapped
    symbol must resolve to itself, not None, so every existing call site
    keeps working with zero code changes.
    """
    service = ScripMasterService()
    assert service.get_shoonya_tsym("NIFTY30JUL2624000CE") == "NIFTY30JUL2624000CE"


def test_get_angel_token_reads_from_in_memory_cache_first():
    service = ScripMasterService()
    service._angel_token_by_symbol["NIFTY30JUL2624000CE"] = "12345"  # noqa: SLF001
    assert service.get_angel_token("NIFTY30JUL2624000CE") == "12345"


def test_get_symbol_for_angel_token_is_the_reverse_lookup():
    service = ScripMasterService()
    service._symbol_by_angel_token["12345"] = "NIFTY30JUL2624000CE"  # noqa: SLF001
    assert service.get_symbol_for_angel_token("12345") == "NIFTY30JUL2624000CE"
    assert service.get_symbol_for_angel_token("unknown") is None
