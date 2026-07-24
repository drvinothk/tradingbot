"""Market Data Service: consumes a BrokerPort's tick/depth stream and
persists normalized rows. Each streaming callback opens its own short-lived
session via `session_scope()` rather than sharing one across threads —
SQLAlchemy sessions aren't safe to use concurrently from multiple threads,
and the mock adapter's (and later, the real WebSocket client's) callbacks
fire from a background thread, not the caller's.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date

from sqlalchemy.orm import Session

from app.core.db.session import session_scope
from app.domain.market.models import DepthSnapshot as DepthSnapshotRow
from app.domain.market.models import IndicatorSnapshot as IndicatorSnapshotRow
from app.domain.market.models import Instrument, OptionContract
from app.domain.market.models import OptionChainSnapshot as OptionChainSnapshotRow
from app.domain.market.models import QuoteTick as QuoteTickRow
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.contracts import DepthSnapshot, Tick
from app.modules.market_data.indicators.engine import IndicatorEngine

logger = logging.getLogger("app.market_data")

_SymbolRef = tuple[str, uuid.UUID]  # ("instrument" | "option_contract", row id)
SessionFactory = Callable[[], AbstractContextManager[Session]]


class MarketDataIngestionService:
    """`session_factory` defaults to the real app-wide `session_scope` (each
    streaming callback fires on a background thread, so it needs its own
    short-lived session rather than sharing a caller-provided one) — but is
    injectable so tests can point it at an isolated test database instead of
    monkeypatching module internals.

    `indicator_engine` is optional — when supplied, every tick for an
    *underlying* (never an option contract; see IndicatorEngine's own docs)
    also updates VWAP/EMA9/EMA20 and persists whatever changed in the same
    transaction as the tick itself.
    """

    def __init__(
        self,
        broker: BrokerPort,
        session_factory: SessionFactory = session_scope,
        indicator_engine: IndicatorEngine | None = None,
    ) -> None:
        self._broker = broker
        self._session_factory = session_factory
        self._indicator_engine = indicator_engine
        self._symbol_map: dict[str, _SymbolRef] = {}

    def _build_symbol_map(self, contract_symbols: list[str]) -> dict[str, _SymbolRef]:
        symbol_map: dict[str, _SymbolRef] = {}
        with self._session_factory() as db:
            for instrument in db.query(Instrument).filter(Instrument.symbol.in_(contract_symbols)):
                symbol_map[instrument.symbol] = ("instrument", instrument.id)
            for contract in db.query(OptionContract).filter(
                OptionContract.symbol.in_(contract_symbols)
            ):
                symbol_map[contract.symbol] = ("option_contract", contract.id)
        return symbol_map

    def start(self, contract_symbols: list[str]) -> None:
        self._symbol_map.update(self._build_symbol_map(contract_symbols))
        unknown = set(contract_symbols) - set(self._symbol_map)
        if unknown:
            logger.warning(
                "subscribe requested for %d symbol(s) not found in instruments/"
                "option_contracts — ticks for them will be silently dropped "
                "until the instrument master is synced: %s",
                len(unknown),
                sorted(unknown),
            )
        self._broker.subscribe_quotes(
            contract_symbols, on_tick=self._on_tick, on_depth=self._on_depth
        )

    def stop(self, contract_symbols: list[str]) -> None:
        self._broker.unsubscribe_quotes(contract_symbols)

    def _on_tick(self, tick: Tick) -> None:
        ref = self._symbol_map.get(tick.contract_symbol)
        if ref is None:
            return
        kind, row_id = ref
        with self._session_factory() as db:
            db.add(
                QuoteTickRow(
                    id=uuid.uuid4(),
                    instrument_id=row_id if kind == "instrument" else None,
                    option_contract_id=row_id if kind == "option_contract" else None,
                    ltp=tick.ltp,
                    bid=tick.bid,
                    ask=tick.ask,
                    volume=tick.volume,
                    oi=tick.oi,
                    ts=tick.ts,
                )
            )

            if kind == "instrument" and self._indicator_engine is not None:
                updated = self._indicator_engine.on_tick(row_id, tick)
                for indicator_name, value in updated.items():
                    db.add(
                        IndicatorSnapshotRow(
                            id=uuid.uuid4(),
                            instrument_id=row_id,
                            indicator_name=indicator_name,
                            timeframe=f"{self._indicator_engine.timeframe_seconds}s",
                            value=value,
                            ts=tick.ts,
                        )
                    )

    def _on_depth(self, depth: DepthSnapshot) -> None:
        ref = self._symbol_map.get(depth.contract_symbol)
        if ref is None or ref[0] != "option_contract":
            # Depth is option-contract-only per the schema (see domain/market/models.py) —
            # an underlying's own order book isn't part of this system's design.
            return
        with self._session_factory() as db:
            db.add(
                DepthSnapshotRow(
                    id=uuid.uuid4(),
                    option_contract_id=ref[1],
                    ts=depth.ts,
                    bid_levels=[
                        {"price": level.price, "qty": level.qty, "orders": level.orders}
                        for level in depth.bid_levels
                    ],
                    ask_levels=[
                        {"price": level.price, "qty": level.qty, "orders": level.orders}
                        for level in depth.ask_levels
                    ],
                )
            )


def record_option_chain_snapshot(
    db_underlying_instrument_id: uuid.UUID,
    broker: BrokerPort,
    underlying_symbol: str,
    expiry: date,
    session_factory: SessionFactory = session_scope,
) -> OptionChainSnapshotRow:
    """One-shot fetch + persist — called on a schedule (Scheduler) or on
    demand, not via the streaming path; option chain snapshots are a
    point-in-time picture, not a per-tick stream.
    """
    chain = broker.get_option_chain(underlying_symbol, expiry)
    with session_factory() as db:
        row = OptionChainSnapshotRow(
            id=uuid.uuid4(),
            instrument_id=db_underlying_instrument_id,
            expiry_date=expiry,
            ts=chain.ts,
            chain_data=[
                {
                    "contract_symbol": e.contract_symbol,
                    "strike": e.strike,
                    "option_type": e.option_type.value,
                    "ltp": e.ltp,
                    "bid": e.bid,
                    "ask": e.ask,
                    "volume": e.volume,
                    "oi": e.oi,
                }
                for e in chain.entries
            ],
        )
        db.add(row)
        db.flush()
        db.refresh(row)
        # session_scope() commits + closes on exit; with the default
        # expire_on_commit=True, the caller touching any attribute afterward
        # would hit a DetachedInstanceError trying to lazily reload from an
        # already-closed session. Expunge so the already-loaded values are
        # kept as-is and no further reload is ever attempted.
        db.expunge(row)
        return row
