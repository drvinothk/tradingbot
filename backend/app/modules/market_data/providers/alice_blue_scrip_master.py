"""Daily Alice Blue contract-master sync — the symbol/token bridge that lets
Alice Blue (market-data only, see `AliceBlueMarketDataProvider`'s own
docstring) resolve this system's existing `Instrument`/`OptionContract` rows
to its own tokens, mirroring `scrip_master.py`'s (Angel One's) identical
role and reasoning almost exactly — same structural (not string) matching by
`(underlying, expiry, strike, option_type)`, same `broker_symbol_map` table,
different `provider` value.

**Confirmed live 2026-08-21** by downloading and inspecting the real files
directly (same "public, no-auth, daily-updated" discipline as Shoonya's own
`NFO_symbols.txt.zip` pipeline and Angel One's own scrip master):

- NFO options: `https://v2api.aliceblueonline.com/restpy/static/contract_master/V2/NFO`
  -> `{"NFO": [{"symbol": "BANKNIFTY", "option_type": "CE", "expiry_date":
  1790640000000, "instrument_type": "OPTIDX", "exchange_segment": "nse_fo",
  "token": "35000", "trading_symbol": "BANKNIFTY29SEP26C72600", "exch":
  "NFO", "lot_size": "30", "strike_price": "72600", "tick_size": "0.05"},
  ...]}`. `expiry_date` is epoch **milliseconds**, `strike_price` is the
  real strike (unlike Angel's own confirmed x100 quirk — no such quirk
  here).
- Index tokens are **not** in the NFO or NSE (cash-market) files at all —
  they're under a third, separate endpoint:
  `https://v2api.aliceblueonline.com/restpy/static/contract_master/V2/INDICES`
  -> `{"NSE": [{"symbol": "NIFTY 50", "token": 26000}, {"symbol": "NIFTY
  BANK", "token": 26009}, ...], "MCX": [...], "BSE": [...]}`. Confirmed:
  `26000`/`26009` are the exact same NSE-assigned index tokens Shoonya's own
  adapter already uses (`ShoonyaBrokerAdapter`'s own known reference
  tokens) — real evidence Alice Blue runs on the same underlying NSE token
  numbering, not a broker-specific scheme, which is also why
  `alice_blue_ws_client.py` can reuse `ShoonyaWSClient`'s exact wire-message
  shapes (`_live_ws` fix, partial-tick merge) almost verbatim.
- The legacy (pre-V2) contract-master download was permanently discontinued
  2025-11-30 per Alice's own docs — only the V2 endpoints above are current.

Our own DB `Instrument.symbol` is `"NIFTY"`/`"BANKNIFTY"` (see
`_KNOWN_UNDERLYINGS` in `api.v1.shoonya`), not Alice's own display name
(`"NIFTY 50"`/`"NIFTY BANK"`) — `_INDEX_DISPLAY_NAME` bridges the two,
scoped to the same two underlyings this system actually trades (matching
`api.v1.shoonya._KNOWN_UNDERLYINGS`'s own scope decision; FINNIFTY is left
out on purpose, same as everywhere else in this codebase today).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

import httpx
from sqlalchemy.orm import Session

from app.core.db.session import SessionFactory, session_scope
from app.domain.market.models import (
    BrokerSymbolMap,
    Instrument,
    MarketDataProviderName,
    OptionContract,
    OptionType,
    ScripMasterSyncLog,
    SyncStatus,
)

logger = logging.getLogger("app.market_data.alice_blue_scrip_master")

_NFO_OPTION_TYPES = frozenset({"OPTIDX"})

# Our DB symbol -> Alice Blue's own INDICES display name. Scoped to what
# this system actually trades today, same as api.v1.shoonya._KNOWN_UNDERLYINGS.
_INDEX_DISPLAY_NAME = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}

RowKind = Literal["index", "option"]


@dataclass(frozen=True)
class AliceBlueScripRow:
    token: str
    tradingsymbol: str
    underlying: str
    kind: RowKind
    expiry: date | None
    strike: float | None
    option_type: OptionType | None
    lot_size: int
    exchange_segment: str
    tick_size: float


def _structural_key(row: AliceBlueScripRow) -> tuple:
    if row.kind == "index":
        return (row.underlying, "index", None, None, None)
    return (row.underlying, "option", row.expiry, row.strike, row.option_type)


def parse_nfo_row(raw: dict, tracked_underlyings: frozenset[str]) -> AliceBlueScripRow | None:
    """One malformed/irrelevant row must not abort the whole file — returns
    `None` (never raises) for anything unparseable or out of scope, same
    per-row-tolerant discipline as `scrip_master.parse_scrip_row`.
    """
    try:
        underlying = str(raw.get("symbol", "")).strip().upper()
        if underlying not in tracked_underlyings:
            return None
        instrument_type = str(raw.get("instrument_type", "")).strip().upper()
        if instrument_type not in _NFO_OPTION_TYPES:
            return None
        option_type_raw = str(raw.get("option_type", "")).strip().upper()
        if option_type_raw not in ("CE", "PE"):
            return None
        option_type = OptionType.CE if option_type_raw == "CE" else OptionType.PE

        token = str(raw["token"])
        tradingsymbol = str(raw["trading_symbol"])
        lot_size = int(float(raw.get("lot_size", 0)))
        tick_size = float(raw.get("tick_size", 0.0))
        strike = float(raw["strike_price"])
        exch = str(raw.get("exch") or raw.get("exchange_segment", "")).strip().upper()

        # Confirmed epoch milliseconds (2026-08-21 live download).
        expiry_ms = int(raw["expiry_date"])
        expiry = datetime.fromtimestamp(expiry_ms / 1000, tz=UTC).date()

        return AliceBlueScripRow(
            token=token,
            tradingsymbol=tradingsymbol,
            underlying=underlying,
            kind="option",
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            lot_size=lot_size,
            exchange_segment=exch or "NFO",
            tick_size=tick_size,
        )
    except (KeyError, ValueError, TypeError):
        logger.warning("Skipping unparseable Alice Blue NFO row: %r", raw)
        return None


def parse_index_row(raw: dict, our_symbol: str) -> AliceBlueScripRow:
    return AliceBlueScripRow(
        token=str(raw["token"]),
        tradingsymbol=str(raw["symbol"]),
        underlying=our_symbol,
        kind="index",
        expiry=None,
        strike=None,
        option_type=None,
        lot_size=1,
        exchange_segment="NSE",
        tick_size=0.05,
    )


class AliceBlueScripMasterService:
    """Owns both the in-memory index (fast path, rebuilt on `fetch_and_parse`)
    and the durable `broker_symbol_map` mirror (`sync_to_db`) — same shape
    as Angel One's `ScripMasterService`, deliberately not shared code (see
    that module's own reasoning for why market-data providers don't share
    adapter internals even when the wire protocol overlaps).
    """

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        nfo_url: str = "https://v2api.aliceblueonline.com/restpy/static/contract_master/V2/NFO",
        indices_url: str = (
            "https://v2api.aliceblueonline.com/restpy/static/contract_master/V2/INDICES"
        ),
        tracked_underlyings: frozenset[str] = frozenset({"NIFTY", "BANKNIFTY"}),
        session_factory: SessionFactory = session_scope,
    ) -> None:
        self._http_client = http_client or httpx.Client(timeout=60.0)
        self._owns_http_client = http_client is None
        self._nfo_url = nfo_url
        self._indices_url = indices_url
        self._tracked_underlyings = tracked_underlyings
        self._session_factory = session_factory

        self._rows_by_key: dict[tuple, AliceBlueScripRow] = {}
        self._all_rows: list[AliceBlueScripRow] = []
        self._token_by_symbol: dict[str, str] = {}
        self._symbol_by_token: dict[str, str] = {}
        self._exchange_by_symbol: dict[str, str] = {}
        self.last_synced_at: datetime | None = None

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    # -- fetch + parse ---------------------------------------------------

    def fetch_and_parse(self) -> int:
        rows: list[AliceBlueScripRow] = []

        try:
            response = self._http_client.get(self._nfo_url)
            response.raise_for_status()
            body = response.json()
            raw_rows = body.get("NFO", []) if isinstance(body, dict) else []
        except (httpx.HTTPError, ValueError):
            logger.exception("Failed to fetch/parse Alice Blue NFO contract master")
            raw_rows = []
        for raw in raw_rows:
            parsed = parse_nfo_row(raw, self._tracked_underlyings)
            if parsed is not None:
                rows.append(parsed)

        try:
            response = self._http_client.get(self._indices_url)
            response.raise_for_status()
            body = response.json()
            nse_rows = body.get("NSE", []) if isinstance(body, dict) else []
        except (httpx.HTTPError, ValueError):
            logger.exception("Failed to fetch/parse Alice Blue INDICES contract master")
            nse_rows = []
        display_to_our_symbol = {v: k for k, v in _INDEX_DISPLAY_NAME.items()}
        for raw in nse_rows:
            display_name = str(raw.get("symbol", "")).strip().upper()
            our_symbol = display_to_our_symbol.get(display_name)
            if our_symbol is None or our_symbol not in self._tracked_underlyings:
                continue
            try:
                rows.append(parse_index_row(raw, our_symbol))
            except (KeyError, ValueError, TypeError):
                logger.warning("Skipping unparseable Alice Blue index row: %r", raw)

        rows_by_key = {_structural_key(row): row for row in rows}
        self._rows_by_key = rows_by_key
        self._all_rows = rows
        return len(rows)

    # -- sync to DB --------------------------------------------------------

    def sync_to_db(self, db: Session) -> ScripMasterSyncLog:
        """Never raises past this function — always records a
        `ScripMasterSyncLog` row, success or failure, same discipline as
        `scrip_master.ScripMasterService.sync_to_db`.
        """
        run_at = datetime.now(UTC)
        rows_mapped = 0
        try:
            instruments = db.query(Instrument).filter(Instrument.is_active.is_(True)).all()
            for instrument in instruments:
                row = self._rows_by_key.get(
                    (instrument.symbol.upper(), "index", None, None, None)
                )
                if row is not None:
                    self._upsert_map(db, instrument, None, row, run_at)
                    rows_mapped += 1

            contracts = db.query(OptionContract).filter(OptionContract.is_active.is_(True)).all()
            for contract in contracts:
                underlying = db.get(Instrument, contract.instrument_id)
                if underlying is None:
                    continue
                row = self._rows_by_key.get(
                    (
                        underlying.symbol.upper(),
                        "option",
                        contract.expiry_date,
                        float(contract.strike),
                        contract.option_type,
                    )
                )
                if row is not None:
                    self._upsert_map(db, None, contract, row, run_at)
                    rows_mapped += 1

            db.flush()
            log = ScripMasterSyncLog(
                id=uuid.uuid4(),
                provider=MarketDataProviderName.ALICE_BLUE,
                run_at=run_at,
                rows_parsed=len(self._all_rows),
                rows_mapped=rows_mapped,
                status=SyncStatus.SUCCESS,
            )
            self.last_synced_at = run_at
        except Exception as exc:  # noqa: BLE001 - a sync job must never die silently-crashed
            log = ScripMasterSyncLog(
                id=uuid.uuid4(),
                provider=MarketDataProviderName.ALICE_BLUE,
                run_at=run_at,
                rows_parsed=len(self._all_rows),
                rows_mapped=rows_mapped,
                status=SyncStatus.FAILED,
                detail=str(exc)[:1000],
            )

        db.add(log)
        db.flush()
        return log

    def _upsert_map(
        self,
        db: Session,
        instrument: Instrument | None,
        option_contract: OptionContract | None,
        row: AliceBlueScripRow,
        run_at: datetime,
    ) -> None:
        query = db.query(BrokerSymbolMap).filter(
            BrokerSymbolMap.provider == MarketDataProviderName.ALICE_BLUE
        )
        if instrument is not None:
            query = query.filter(BrokerSymbolMap.instrument_id == instrument.id)
        else:
            assert option_contract is not None
            query = query.filter(BrokerSymbolMap.option_contract_id == option_contract.id)
        db_row = query.one_or_none()
        if db_row is None:
            db_row = BrokerSymbolMap(
                id=uuid.uuid4(),
                instrument_id=instrument.id if instrument is not None else None,
                option_contract_id=option_contract.id if option_contract is not None else None,
                provider=MarketDataProviderName.ALICE_BLUE,
                external_symbol=row.tradingsymbol,
                external_token=row.token,
                exchange=row.exchange_segment,
                synced_at=run_at,
            )
            db.add(db_row)
        else:
            db_row.external_symbol = row.tradingsymbol
            db_row.external_token = row.token
            db_row.exchange = row.exchange_segment
            db_row.synced_at = run_at
        db.flush()

        our_symbol = instrument.symbol if instrument is not None else option_contract.symbol  # type: ignore[union-attr]
        self._token_by_symbol[our_symbol] = row.token
        self._symbol_by_token[row.token] = our_symbol
        self._exchange_by_symbol[our_symbol] = row.exchange_segment

    # -- lookups -------------------------------------------------------------

    def get_token(self, symbol: str) -> str | None:
        token = self._token_by_symbol.get(symbol)
        if token is not None:
            return token
        with self._session_factory() as db:
            return self._lookup_token_from_db(db, symbol)

    def get_symbol_for_token(self, token: str) -> str | None:
        return self._symbol_by_token.get(token)

    def get_exchange_segment(self, symbol: str) -> str | None:
        return self._exchange_by_symbol.get(symbol)

    def _lookup_token_from_db(self, db: Session, symbol: str) -> str | None:
        instrument = db.query(Instrument).filter(Instrument.symbol == symbol).one_or_none()
        if instrument is not None:
            row = (
                db.query(BrokerSymbolMap)
                .filter(
                    BrokerSymbolMap.instrument_id == instrument.id,
                    BrokerSymbolMap.provider == MarketDataProviderName.ALICE_BLUE,
                )
                .one_or_none()
            )
            return row.external_token if row is not None else None

        contract = db.query(OptionContract).filter(OptionContract.symbol == symbol).one_or_none()
        if contract is not None:
            row = (
                db.query(BrokerSymbolMap)
                .filter(
                    BrokerSymbolMap.option_contract_id == contract.id,
                    BrokerSymbolMap.provider == MarketDataProviderName.ALICE_BLUE,
                )
                .one_or_none()
            )
            return row.external_token if row is not None else None
        return None
