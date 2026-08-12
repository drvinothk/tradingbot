"""Shoonya's own static NFO scrip-master parsing — see
`shoonya.scrip_master`'s own docstring for the real row shape this is built
from (confirmed 2026-08-12 by downloading and inspecting the actual file,
not assumed).
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import httpx

from app.modules.broker_adapter.base.contracts import OptionType
from app.modules.broker_adapter.shoonya.scrip_master import (
    download_nfo_scrip_master,
    parse_nfo_scrip_master,
    parse_shoonya_scrip_expiry,
)

_HEADER = (
    "Exchange,Token,LotSize,Symbol,TradingSymbol,Expiry,Instrument,OptionType,StrikePrice,TickSize"
)


def _zip_bytes(csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("NFO_symbols.txt", csv_text)
    return buf.getvalue()


def test_parse_shoonya_scrip_expiry():
    assert parse_shoonya_scrip_expiry("18-AUG-2026") == date(2026, 8, 18)


def test_parses_a_real_shaped_nifty_row():
    csv_text = "\n".join(
        [
            _HEADER,
            "NFO,48407,65,NIFTY,NIFTY18AUG26C18550,18-AUG-2026,OPTIDX,CE,18550,0.05",
        ]
    )
    infos = parse_nfo_scrip_master(_zip_bytes(csv_text))

    assert len(infos) == 1
    info = infos[0]
    assert info.symbol == "NIFTY18AUG26C18550"
    assert info.exchange == "NFO"
    assert info.is_option is True
    assert info.underlying == "NIFTY"
    assert info.expiry == date(2026, 8, 18)
    assert info.strike == 18550.0  # real strike, not x100 like Angel's own file
    assert info.option_type == OptionType.CE
    assert info.broker_token == "48407"
    assert info.lot_size == 65
    assert info.tick_size == 0.05


def test_excludes_stock_options_and_untracked_underlyings():
    csv_text = "\n".join(
        [
            _HEADER,
            # Real stock option -- vastly outnumbers index options in the
            # real file, must never be synced (this system only trades
            # NIFTY/BANKNIFTY index options).
            "NFO,156871,900,ZYDUSLIFE,ZYDUSLIFE29SEP26P1600,29-SEP-2026,OPTSTK,PE,1600,0.05",
            # Real underlying, but not one this system trades.
            "NFO,1,25,FINNIFTY,FINNIFTY18AUG26C20000,18-AUG-2026,OPTIDX,CE,20000,0.05",
        ]
    )
    infos = parse_nfo_scrip_master(_zip_bytes(csv_text))
    assert infos == []


def test_excludes_futures_and_reserved_placeholder_rows():
    csv_text = "\n".join(
        [
            _HEADER,
            "NFO,2,65,NIFTY,NIFTY25AUG26F,25-AUG-2026,FUTIDX,,0,0",
            # Real reserved/delisted-token placeholder shape from the actual
            # file -- every field but Exchange/Token blank.
            "NFO,48577,,,,,,,0,0",
        ]
    )
    infos = parse_nfo_scrip_master(_zip_bytes(csv_text))
    assert infos == []


def test_one_malformed_row_does_not_abort_the_whole_parse():
    csv_text = "\n".join(
        [
            _HEADER,
            "NFO,1,65,NIFTY,NIFTY18AUG26C18500,not-a-date,OPTIDX,CE,18500,0.05",
            "NFO,2,65,NIFTY,NIFTY18AUG26P18500,18-AUG-2026,OPTIDX,PE,18500,0.05",
        ]
    )
    infos = parse_nfo_scrip_master(_zip_bytes(csv_text))
    assert len(infos) == 1
    assert infos[0].symbol == "NIFTY18AUG26P18500"


def test_bad_zip_returns_empty_list_not_an_exception():
    assert parse_nfo_scrip_master(b"not a zip file") == []


def test_zip_with_no_txt_member_returns_empty_list():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.md", "nothing useful here")
    assert parse_nfo_scrip_master(buf.getvalue()) == []


class _FakeTransport(httpx.BaseTransport):
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._response


def test_download_returns_the_response_body_on_success():
    body = _zip_bytes(_HEADER)
    client = httpx.Client(transport=_FakeTransport(httpx.Response(200, content=body)))
    assert download_nfo_scrip_master(client) == body


def test_download_returns_none_on_http_error():
    client = httpx.Client(transport=_FakeTransport(httpx.Response(500)))
    assert download_nfo_scrip_master(client) is None


def test_download_returns_none_on_connection_error():
    class _RaisingTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated network failure")

    client = httpx.Client(transport=_RaisingTransport())
    assert download_nfo_scrip_master(client) is None
