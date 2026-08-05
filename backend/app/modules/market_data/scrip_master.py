"""Daily Angel One scrip-master sync — the symbol/token bridge that lets a
*second* market-data provider (Angel One) resolve this system's existing
`Instrument`/`OptionContract` rows (populated today by Shoonya's own
`get_instrument_master`, see `scheduler/instrument_sync.py`) to its own
tokens, without changing what those rows' `symbol` columns mean or touching
execution's own symbol handling at all.

**Matching is structural, not string-based**: Angel and Shoonya each use their
own tradingsymbol convention for the same real contract, so a row is matched
by `(underlying, expiry, strike, option_type)` against our existing DB rows,
never by comparing symbol strings directly.

**Two confirmed schema quirks** (from Angel's own scrip-master row shape,
`{"token": "58784", "symbol": "NIFTY28OCT2524400CE", "name": "NIFTY",
"expiry": "28OCT2025", "strike": "2440000.000000", "lotsize": "75",
"instrumenttype": "OPTIDX", "exch_seg": "NFO", "tick_size": "5.000000"}`):
`strike` is the real strike price multiplied by 100 as a string (divided by
100 here); `expiry` is `DDMMMYYYY` with no separators (`parse_angel_expiry`).

**One unconfirmed carve-out, flagged rather than silently assumed**: the
schema handed to this module has no discrete CE/PE field, so `option_type`
is derived from the trading symbol's own `CE`/`PE` suffix — a targeted
suffix check, not a parse of the whole composite symbol, and reliable per
Angel's own documented tradingsymbol convention, but worth re-confirming
against a real download before trusting it blindly.

**The underlying index token itself (NIFTY/BANKNIFTY/FINNIFTY spot) is not
in the `NFO` segment** (NFO is derivatives-only) — this module also accepts
`NSE`-segment rows matching a tracked underlying name with no strike, on the
same "index it, don't guess a broker's undocumented behavior" caution as the
rest of this codebase's live-broker work. Exact field values for that row
(`instrumenttype` for an index) are unconfirmed against a live download —
flagged in `_parse_row`, first thing to check if the underlying itself never
maps.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

import httpx
from sqlalchemy.orm import Session

from app.core.db.session import session_scope
from app.domain.market.models import (
    BrokerSymbolMap,
    Instrument,
    MarketDataProviderName,
    OptionContract,
    OptionType,
    ScripMasterSyncLog,
    SyncStatus,
)

logger = logging.getLogger("app.market_data.scrip_master")

SessionFactory = Callable[[], AbstractContextManager[Session]]

# NFO is options+futures; these are indexed even though this system doesn't
# trade futures today (see module docstring's "index it, don't have to use
# it yet" reasoning, matching FINNIFTY's own treatment).
_TRACKED_UNDERLYINGS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY"})
_NFO_OPTION_TYPES = frozenset({"OPTIDX"})
_NFO_FUTURE_TYPES = frozenset({"FUTIDX"})

# Unconfirmed against a live download — see module docstring's index-row caveat.
_INDEX_INSTRUMENT_TYPES = frozenset({"", "INDEX", "AMXIDX"})

_ANGELONE_SCRIP_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
# Domain-rebrand fallback (angelbroking.com -> angelone.in) — tried second if
# the primary 404s, same "flagged, not silently picked" treatment
# ShoonyaSettings.api_host's own docstring gives its own unconfirmed-host
# discrepancy.
_ANGELONE_SCRIP_MASTER_URL_FALLBACK = (
    "https://margincalculator.angelbroking.com/OpenAPI_MasterData/OpenAPIScripMaster.json"
)

RowKind = Literal["index", "option", "future"]


class ScripMasterParseError(Exception):
    pass


@dataclass(frozen=True)
class AngelScripRow:
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


def parse_angel_expiry(raw: str) -> date:
    """`"28OCT2025"` -> `date(2025, 10, 28)` — Angel's own `DDMMMYYYY` format,
    no separators. Distinct from Shoonya's own date format
    (`normalizer.parse_shoonya_date`) since the two brokers don't agree.
    """
    try:
        return datetime.strptime(raw.strip().upper(), "%d%b%Y").date()
    except ValueError as exc:
        raise ScripMasterParseError(f"unparseable Angel expiry {raw!r}") from exc


def parse_scrip_row(raw: dict) -> AngelScripRow | None:
    """One malformed/irrelevant row must not abort the whole file — returns
    `None` (never raises) for anything unparseable or out of scope, same
    per-row-tolerant discipline as
    `ShoonyaBrokerAdapter.get_instrument_master`.
    """
    try:
        name = str(raw.get("name", "")).strip().upper()
        if name not in _TRACKED_UNDERLYINGS:
            return None
        exch_seg = str(raw.get("exch_seg", "")).strip().upper()
        instrument_type = str(raw.get("instrumenttype", "")).strip().upper()
        token = str(raw["token"])
        tradingsymbol = str(raw["symbol"])
        lot_size = int(float(raw.get("lotsize", 0)))
        tick_size = float(raw.get("tick_size", 0.0))

        if exch_seg == "NFO" and instrument_type in _NFO_OPTION_TYPES:
            # Confirmed quirk: strike is the real strike x 100, as a string.
            strike = float(raw["strike"]) / 100.0
            if tradingsymbol.endswith("CE"):
                option_type = OptionType.CE
            elif tradingsymbol.endswith("PE"):
                option_type = OptionType.PE
            else:
                return None
            expiry = parse_angel_expiry(str(raw.get("expiry", "")))
            return AngelScripRow(
                token=token,
                tradingsymbol=tradingsymbol,
                underlying=name,
                kind="option",
                expiry=expiry,
                strike=strike,
                option_type=option_type,
                lot_size=lot_size,
                exchange_segment=exch_seg,
                tick_size=tick_size,
            )

        if exch_seg == "NFO" and instrument_type in _NFO_FUTURE_TYPES:
            expiry = parse_angel_expiry(str(raw.get("expiry", "")))
            return AngelScripRow(
                token=token,
                tradingsymbol=tradingsymbol,
                underlying=name,
                kind="future",
                expiry=expiry,
                strike=None,
                option_type=None,
                lot_size=lot_size,
                exchange_segment=exch_seg,
                tick_size=tick_size,
            )

        if exch_seg == "NSE" and instrument_type in _INDEX_INSTRUMENT_TYPES:
            return AngelScripRow(
                token=token,
                tradingsymbol=tradingsymbol,
                underlying=name,
                kind="index",
                expiry=None,
                strike=None,
                option_type=None,
                lot_size=lot_size,
                exchange_segment=exch_seg,
                tick_size=tick_size,
            )

        return None
    except (KeyError, ValueError, TypeError, ScripMasterParseError):
        logger.warning("Skipping unparseable Angel scrip master row: %r", raw)
        return None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _structural_key(row: AngelScripRow) -> tuple:
    if row.kind == "index":
        return (row.underlying, "index", None, None, None)
    return (row.underlying, row.kind, row.expiry, row.strike, row.option_type)


class ScripMasterService:
    """Owns both the in-memory index (fast path, rebuilt on `fetch_and_parse`)
    and the durable `broker_symbol_map` mirror (`sync_to_db`) — a fresh
    process can serve `get_angel_token`/`get_shoonya_tsym` from the DB mirror
    immediately after startup, before the first scheduled refresh completes.
    """

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        primary_url: str = _ANGELONE_SCRIP_MASTER_URL,
        fallback_url: str = _ANGELONE_SCRIP_MASTER_URL_FALLBACK,
        session_factory: SessionFactory = session_scope,
    ) -> None:
        self._http_client = http_client or httpx.Client(timeout=60.0)
        self._owns_http_client = http_client is None
        self._primary_url = primary_url
        self._fallback_url = fallback_url
        self._session_factory = session_factory

        self._rows_by_key: dict[tuple, AngelScripRow] = {}
        self._all_rows: list[AngelScripRow] = []
        self._angel_token_by_symbol: dict[str, str] = {}
        self._symbol_by_angel_token: dict[str, str] = {}
        self._angel_exchange_by_symbol: dict[str, str] = {}
        self._shoonya_symbol_by_symbol: dict[str, str] = {}
        self.last_synced_at: datetime | None = None

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    # -- fetch + parse ---------------------------------------------------

    def _download(self) -> list[dict]:
        for url in (self._primary_url, self._fallback_url):
            if not url:
                continue
            try:
                response = self._http_client.get(url)
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError):
                logger.exception("Failed to fetch/parse Angel scrip master from %s", url)
                continue
            if isinstance(data, list):
                return data
            logger.warning(
                "Unexpected Angel scrip master response shape from %s: %r", url, type(data)
            )
        return []

    def fetch_and_parse(self) -> int:
        """Downloads + parses the master file, rebuilding the in-memory
        structural index used by `sync_to_db`. Returns the count of rows
        recognized (NIFTY/BANKNIFTY/FINNIFTY NFO+index rows) — everything
        else in the file is discarded immediately, not retained, to keep
        memory bounded against a file listing every NSE/BSE/MCX instrument.
        """
        raw_rows = self._download()
        rows_by_key: dict[tuple, AngelScripRow] = {}
        all_rows: list[AngelScripRow] = []
        for raw in raw_rows:
            parsed = parse_scrip_row(raw)
            if parsed is None:
                continue
            all_rows.append(parsed)
            rows_by_key[_structural_key(parsed)] = parsed
        self._rows_by_key = rows_by_key
        self._all_rows = all_rows
        return len(all_rows)

    # -- sync to DB --------------------------------------------------------

    def sync_to_db(self, db: Session) -> ScripMasterSyncLog:
        """For every *existing, active* `Instrument`/`OptionContract` row,
        looks up the matching Angel row by structural key and upserts a
        `broker_symbol_map` row (provider=angel_one). Also upserts a
        provider=shoonya passthrough row (our own DB symbol is already the
        Shoonya tsym) for symmetry — a future execution-broker swap reuses
        this exact mechanism rather than inventing a second one. Never
        raises past this function: always records a `ScripMasterSyncLog`
        row, success or failure.
        """
        run_at = _utcnow()
        rows_mapped = 0
        try:
            instruments = db.query(Instrument).filter(Instrument.is_active.is_(True)).all()
            for instrument in instruments:
                angel_row = self._rows_by_key.get(
                    (instrument.symbol.upper(), "index", None, None, None)
                )
                if angel_row is not None:
                    self._upsert_map(db, instrument, None, angel_row, run_at)
                    rows_mapped += 1
                self._upsert_shoonya_passthrough(db, instrument, None, run_at)

            contracts = (
                db.query(OptionContract).filter(OptionContract.is_active.is_(True)).all()
            )
            for contract in contracts:
                underlying = db.get(Instrument, contract.instrument_id)
                if underlying is None:
                    continue
                angel_row = self._rows_by_key.get(
                    (
                        underlying.symbol.upper(),
                        "option",
                        contract.expiry_date,
                        float(contract.strike),
                        contract.option_type,
                    )
                )
                if angel_row is not None:
                    self._upsert_map(db, None, contract, angel_row, run_at)
                    rows_mapped += 1
                self._upsert_shoonya_passthrough(db, None, contract, run_at)

            db.flush()
            log = ScripMasterSyncLog(
                id=uuid.uuid4(),
                provider=MarketDataProviderName.ANGEL_ONE,
                run_at=run_at,
                rows_parsed=len(self._all_rows),
                rows_mapped=rows_mapped,
                status=SyncStatus.SUCCESS,
            )
            self.last_synced_at = run_at
        except Exception as exc:  # noqa: BLE001 - a sync job must never die silently-crashed
            log = ScripMasterSyncLog(
                id=uuid.uuid4(),
                provider=MarketDataProviderName.ANGEL_ONE,
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
        angel_row: AngelScripRow,
        run_at: datetime,
    ) -> None:
        query = db.query(BrokerSymbolMap).filter(
            BrokerSymbolMap.provider == MarketDataProviderName.ANGEL_ONE
        )
        if instrument is not None:
            query = query.filter(BrokerSymbolMap.instrument_id == instrument.id)
        else:
            assert option_contract is not None
            query = query.filter(BrokerSymbolMap.option_contract_id == option_contract.id)
        row = query.one_or_none()
        if row is None:
            row = BrokerSymbolMap(
                id=uuid.uuid4(),
                instrument_id=instrument.id if instrument is not None else None,
                option_contract_id=option_contract.id if option_contract is not None else None,
                provider=MarketDataProviderName.ANGEL_ONE,
                external_symbol=angel_row.tradingsymbol,
                external_token=angel_row.token,
                exchange=angel_row.exchange_segment,
                synced_at=run_at,
            )
            db.add(row)
        else:
            row.external_symbol = angel_row.tradingsymbol
            row.external_token = angel_row.token
            row.exchange = angel_row.exchange_segment
            row.synced_at = run_at
        db.flush()

        our_symbol = instrument.symbol if instrument is not None else option_contract.symbol  # type: ignore[union-attr]
        self._angel_token_by_symbol[our_symbol] = angel_row.token
        self._symbol_by_angel_token[angel_row.token] = our_symbol
        self._angel_exchange_by_symbol[our_symbol] = angel_row.exchange_segment

    def _upsert_shoonya_passthrough(
        self,
        db: Session,
        instrument: Instrument | None,
        option_contract: OptionContract | None,
        run_at: datetime,
    ) -> None:
        our_symbol = instrument.symbol if instrument is not None else option_contract.symbol  # type: ignore[union-attr]
        our_token = "" if instrument is not None else option_contract.broker_token  # type: ignore[union-attr]
        exchange = instrument.exchange if instrument is not None else "NFO"

        query = db.query(BrokerSymbolMap).filter(
            BrokerSymbolMap.provider == MarketDataProviderName.SHOONYA
        )
        if instrument is not None:
            query = query.filter(BrokerSymbolMap.instrument_id == instrument.id)
        else:
            assert option_contract is not None
            query = query.filter(BrokerSymbolMap.option_contract_id == option_contract.id)
        row = query.one_or_none()
        if row is None:
            row = BrokerSymbolMap(
                id=uuid.uuid4(),
                instrument_id=instrument.id if instrument is not None else None,
                option_contract_id=option_contract.id if option_contract is not None else None,
                provider=MarketDataProviderName.SHOONYA,
                external_symbol=our_symbol,
                external_token=our_token,
                exchange=exchange,
                synced_at=run_at,
            )
            db.add(row)
        else:
            row.external_symbol = our_symbol
            row.external_token = our_token
            row.exchange = exchange
            row.synced_at = run_at
        db.flush()
        self._shoonya_symbol_by_symbol[our_symbol] = our_symbol

    # -- lookups -------------------------------------------------------------

    def get_angel_token(self, symbol: str) -> str | None:
        token = self._angel_token_by_symbol.get(symbol)
        if token is not None:
            return token
        with self._session_factory() as db:
            return self._lookup_token_from_db(db, symbol, MarketDataProviderName.ANGEL_ONE)

    def get_symbol_for_angel_token(self, token: str) -> str | None:
        """Reverse lookup: an incoming Angel tick's own token -> our DB
        symbol, so `AngelOneMarketDataProvider` can emit a `Tick.contract_symbol`
        that `MarketDataIngestionService`'s existing symbol matching already
        recognizes. In-memory only — a token that arrives before this
        process has ever synced can't be resolved and is dropped by the
        caller, same as `MarketDataIngestionService._on_tick`'s existing
        "unknown symbol, drop the tick" behavior.
        """
        return self._symbol_by_angel_token.get(token)

    def get_angel_exchange_segment(self, symbol: str) -> str | None:
        """`"NSE"` for an underlying index token, `"NFO"` for an option
        contract — `AngelWSClient.subscribe`/`get_price_history` need this to
        pick the right SmartStream `exchangeType` code. In-memory only, same
        "populated by sync, not persisted separately" reasoning as the token
        maps above — `broker_symbol_map.exchange` is the durable copy for a
        cold-start DB fallback, not read here directly to keep this a single
        fast in-memory path for the hot subscribe call.
        """
        return self._angel_exchange_by_symbol.get(symbol)

    def get_shoonya_tsym(self, symbol: str) -> str:
        """Our own DB `symbol` already *is* the Shoonya tsym today (see
        module docstring) — this still checks `broker_symbol_map` first so a
        future execution-broker swap can populate a genuinely different
        mapping here without any caller needing to change; falls back to
        `symbol` unchanged (today's actual truth) rather than `None` when
        nothing's explicitly mapped yet.
        """
        mapped = self._shoonya_symbol_by_symbol.get(symbol)
        if mapped is not None:
            return mapped
        return symbol

    def _lookup_token_from_db(
        self, db: Session, symbol: str, provider: MarketDataProviderName
    ) -> str | None:
        instrument = db.query(Instrument).filter(Instrument.symbol == symbol).one_or_none()
        if instrument is not None:
            row = (
                db.query(BrokerSymbolMap)
                .filter(
                    BrokerSymbolMap.instrument_id == instrument.id,
                    BrokerSymbolMap.provider == provider,
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
                    BrokerSymbolMap.provider == provider,
                )
                .one_or_none()
            )
            return row.external_token if row is not None else None
        return None
