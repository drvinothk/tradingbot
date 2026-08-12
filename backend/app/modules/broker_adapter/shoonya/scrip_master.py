"""Shoonya's own official static NFO scrip master
(`https://api.shoonya.com/NFO_symbols.txt.zip`) — a real, public, no-auth,
daily-updated file, confirmed by downloading and inspecting it directly
(2026-08-12), not assumed. Built as the replacement primary source for
`ShoonyaBrokerAdapter.get_instrument_master`'s NFO path, demoting the old
live `SearchScrip` loop to a fallback: live evidence this same session
showed `SearchScrip` returning different, non-overlapping expiry subsets
across separate calls for the same underlying, and an empty `token` field
for every recently-synced option row — a static, complete, correctly-
tokened daily snapshot has neither failure mode.

**Confirmed real row shape** (CSV inside the zip, one header row):
`Exchange,Token,LotSize,Symbol,TradingSymbol,Expiry,Instrument,OptionType,
StrikePrice,TickSize` — e.g.
`NFO,48407,65,NIFTY,NIFTY18AUG26C18550,18-AUG-2026,OPTIDX,CE,18550,0.05`.
Two things this format gets right that Angel One's own scrip master (see
`market_data/scrip_master.py`'s docstring) doesn't: `StrikePrice` is the
real strike, not the real strike x 100 (no /100 needed here), and there's
an explicit `OptionType` column (`CE`/`PE`), so no suffix-guessing on the
trading symbol is needed the way Angel's parser has to. `Expiry` is
`DD-MMM-YYYY` (hyphens), distinct from both Angel's `DDMMMYYYY` (no
separators) and Shoonya's own REST API date convention
(`normalizer.parse_shoonya_date`) — none of these three agree with each
other, confirmed independently for each.

Many rows in the file have every field but `Exchange`/`Token` blank
(reserved/delisted token placeholders) — `parse_nfo_scrip_master` skips
these, and any other row, exactly like `Instrument` values other than
`OPTIDX` (stock options `OPTSTK` vastly outnumber index options in this
file, futures `FUTIDX`/`FUTSTK`), silently and per-row, never aborting the
whole parse over one bad row -- same discipline
`ShoonyaBrokerAdapter.get_instrument_master`'s existing `SearchScrip` path
and Angel's own `parse_scrip_row` both already follow.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import date, datetime

import httpx

from app.modules.broker_adapter.base.contracts import InstrumentInfo, OptionType

logger = logging.getLogger("app.broker_adapter.shoonya.scrip_master")

_NFO_SCRIP_MASTER_URL = "https://api.shoonya.com/NFO_symbols.txt.zip"
_TRACKED_UNDERLYINGS = frozenset({"NIFTY", "BANKNIFTY"})
_TRACKED_INSTRUMENT_TYPES = frozenset({"OPTIDX"})


def parse_shoonya_scrip_expiry(raw: str) -> date:
    """`"18-AUG-2026"` -> `date(2026, 8, 18)` — this file's own `DD-MMM-YYYY`
    format, confirmed against a real download, distinct from both Angel's
    and Shoonya's REST API's own separate date conventions (see module
    docstring).
    """
    return datetime.strptime(raw.strip().upper(), "%d-%b-%Y").date()


def _parse_row(row: dict[str, str]) -> InstrumentInfo | None:
    try:
        symbol = row.get("Symbol", "").strip().upper()
        if symbol not in _TRACKED_UNDERLYINGS:
            return None
        instrument_type = row.get("Instrument", "").strip().upper()
        if instrument_type not in _TRACKED_INSTRUMENT_TYPES:
            return None

        option_type_raw = row.get("OptionType", "").strip().upper()
        if option_type_raw not in ("CE", "PE"):
            return None
        option_type = OptionType(option_type_raw)

        token = row.get("Token", "").strip()
        trading_symbol = row.get("TradingSymbol", "").strip()
        if not token or not trading_symbol:
            return None

        expiry = parse_shoonya_scrip_expiry(row.get("Expiry", ""))
        strike = float(row.get("StrikePrice", ""))
        lot_size = int(float(row.get("LotSize", "0")))
        tick_size = float(row.get("TickSize", "0"))

        return InstrumentInfo(
            symbol=trading_symbol,
            exchange="NFO",
            lot_size=lot_size,
            tick_size=tick_size,
            is_option=True,
            underlying=symbol,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            broker_token=token,
        )
    except (KeyError, ValueError, TypeError):
        logger.warning("Skipping unparseable Shoonya scrip master row: %r", row)
        return None


def parse_nfo_scrip_master(zip_bytes: bytes) -> list[InstrumentInfo]:
    """Unzips and parses in-memory — the real file is ~650KB zipped, ~78k
    rows, of which only NIFTY/BANKNIFTY `OPTIDX` rows (a few hundred) are
    kept; the rest is discarded immediately rather than retained, same
    bounded-memory reasoning as Angel's own `fetch_and_parse`. Returns an
    empty list (never raises) on any zip/CSV-level failure — the caller
    (`ShoonyaBrokerAdapter.get_instrument_master`) treats an empty result
    as "fall back to SearchScrip", so a corrupt or unreachable file
    degrades to today's existing behavior rather than crashing a sync.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
            if not names:
                logger.warning("Shoonya scrip master zip had no .txt member: %r", zf.namelist())
                return []
            raw_text = zf.read(names[0]).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        logger.exception("Shoonya scrip master response was not a valid zip")
        return []

    reader = csv.DictReader(io.StringIO(raw_text))
    infos: list[InstrumentInfo] = []
    for row in reader:
        parsed = _parse_row(row)
        if parsed is not None:
            infos.append(parsed)
    return infos


def download_nfo_scrip_master(http_client: httpx.Client) -> bytes | None:
    """Returns `None` (never raises) on any network failure — same
    fall-back-safe contract as `parse_nfo_scrip_master`.
    """
    try:
        response = http_client.get(_NFO_SCRIP_MASTER_URL, timeout=30.0)
        response.raise_for_status()
        return response.content
    except httpx.HTTPError:
        logger.exception(
            "Failed to download Shoonya NFO scrip master from %s", _NFO_SCRIP_MASTER_URL
        )
        return None
