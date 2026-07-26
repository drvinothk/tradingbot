"""Market/instrument domain. `QuoteTick` and `IndicatorSnapshot` both carry
two nullable FKs (instrument_id, option_contract_id) with the convention that
exactly one is set — VWAP/EMA indicators are computed on the *underlying*
(e.g. NIFTY spot/futures), not on individual option premiums, so the
indicator engine needs underlying ticks stored the same way as option ticks.
`DepthSnapshot` stays option-contract-only: the trading framework's market-
depth signal is specifically about the option order book, not the index.
`PriceBar` is instrument-only for the same reason `IndicatorSnapshot` is:
Phase 4's strategies need real completed-candle O/H/L/C (opening-range,
pullback, confirmation-candle structure), not just the EMA/VWAP scalar
`IndicatorSnapshot` already carries — populated from the same completed `Bar`
object `IndicatorEngine.on_tick` builds internally, which Phase 1-3 discarded
once the EMA value was computed.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TimestampMixin, UUIDPkMixin


class OptionType(enum.StrEnum):
    CE = "CE"
    PE = "PE"


class SyncStatus(enum.StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class Instrument(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String(60))
    exchange: Mapped[str] = mapped_column(String(20))
    lot_size: Mapped[int] = mapped_column(Integer)
    tick_size: Mapped[float] = mapped_column(Numeric(10, 4))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("symbol", "exchange", name="uq_instrument_symbol_exchange"),)


class OptionContract(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "option_contracts"

    instrument_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instruments.id"))
    expiry_date: Mapped[date] = mapped_column(Date)
    strike: Mapped[float] = mapped_column(Numeric(12, 2))
    option_type: Mapped[OptionType] = mapped_column(String(2))
    # The tradable symbol string as returned by the broker's instrument master
    # (InstrumentInfo.symbol) — this is what QuoteTick/DepthSnapshot ingestion
    # matches incoming tick.contract_symbol against. Distinct from
    # broker_token (the broker's internal numeric token, not always present).
    symbol: Mapped[str] = mapped_column(String(60))
    broker_token: Mapped[str] = mapped_column(String(40), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "expiry_date",
            "strike",
            "option_type",
            name="uq_option_contract_identity",
        ),
        UniqueConstraint("symbol", name="uq_option_contract_symbol"),
        Index("ix_option_contracts_instrument_expiry", "instrument_id", "expiry_date"),
    )


class InstrumentMasterSyncLog(Base, UUIDPkMixin):
    __tablename__ = "instrument_master_sync_log"

    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    instruments_updated: Mapped[int] = mapped_column(Integer, default=0)
    contracts_added: Mapped[int] = mapped_column(Integer, default=0)
    contracts_expired: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[SyncStatus] = mapped_column(String(20))
    detail: Mapped[str] = mapped_column(String(1000), default="")


class QuoteTick(Base, UUIDPkMixin):
    __tablename__ = "quote_ticks"

    instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=True
    )
    option_contract_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("option_contracts.id"), nullable=True
    )
    ltp: Mapped[float] = mapped_column(Numeric(12, 4))
    bid: Mapped[float] = mapped_column(Numeric(12, 4))
    ask: Mapped[float] = mapped_column(Numeric(12, 4))
    volume: Mapped[int] = mapped_column(Integer)
    oi: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "(instrument_id IS NOT NULL) <> (option_contract_id IS NOT NULL)",
            name="ck_quote_tick_exactly_one_reference",
        ),
        Index("ix_quote_ticks_instrument_ts", "instrument_id", "ts"),
        Index("ix_quote_ticks_option_contract_ts", "option_contract_id", "ts"),
    )


class DepthSnapshot(Base, UUIDPkMixin):
    __tablename__ = "depth_snapshots"

    option_contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("option_contracts.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bid_levels: Mapped[list] = mapped_column(JSONB)
    ask_levels: Mapped[list] = mapped_column(JSONB)

    __table_args__ = (Index("ix_depth_snapshots_option_contract_ts", "option_contract_id", "ts"),)


class OptionChainSnapshot(Base, UUIDPkMixin):
    __tablename__ = "option_chain_snapshots"

    instrument_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instruments.id"))
    expiry_date: Mapped[date] = mapped_column(Date)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    chain_data: Mapped[dict] = mapped_column(JSONB)

    __table_args__ = (
        Index(
            "ix_option_chain_snapshots_instrument_expiry_ts",
            "instrument_id",
            "expiry_date",
            "ts",
        ),
    )


class IndicatorSnapshot(Base, UUIDPkMixin):
    __tablename__ = "indicator_snapshots"

    instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=True
    )
    option_contract_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("option_contracts.id"), nullable=True
    )
    indicator_name: Mapped[str] = mapped_column(String(30))
    timeframe: Mapped[str] = mapped_column(String(10))
    value: Mapped[float] = mapped_column(Numeric(14, 4))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "(instrument_id IS NOT NULL) <> (option_contract_id IS NOT NULL)",
            name="ck_indicator_snapshot_exactly_one_reference",
        ),
        Index(
            "ix_indicator_snapshots_instrument",
            "instrument_id",
            "indicator_name",
            "timeframe",
            "ts",
        ),
        Index(
            "ix_indicator_snapshots_option_contract",
            "option_contract_id",
            "indicator_name",
            "timeframe",
            "ts",
        ),
    )


class PriceBar(Base, UUIDPkMixin):
    __tablename__ = "price_bars"

    instrument_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instruments.id"))
    timeframe: Mapped[str] = mapped_column(String(10))
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[float] = mapped_column(Numeric(12, 4))
    high: Mapped[float] = mapped_column(Numeric(12, 4))
    low: Mapped[float] = mapped_column(Numeric(12, 4))
    close: Mapped[float] = mapped_column(Numeric(12, 4))
    volume: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "timeframe", "bucket_start", name="uq_price_bar_bucket"
        ),
        Index(
            "ix_price_bars_instrument_timeframe_bucket",
            "instrument_id",
            "timeframe",
            "bucket_start",
        ),
    )
