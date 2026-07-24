"""market/instrument domain: instruments, option contracts, ticks, depth, chain, indicators

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-24

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("symbol", sa.String(60), nullable=False),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("tick_size", sa.Numeric(10, 4), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("symbol", "exchange", name="uq_instrument_symbol_exchange"),
    )

    op.create_table(
        "option_contracts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            nullable=False,
        ),
        sa.Column("expiry_date", sa.Date(), nullable=False),
        sa.Column("strike", sa.Numeric(12, 2), nullable=False),
        sa.Column("option_type", sa.String(2), nullable=False),
        sa.Column("symbol", sa.String(60), nullable=False),
        sa.Column("broker_token", sa.String(40), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "instrument_id",
            "expiry_date",
            "strike",
            "option_type",
            name="uq_option_contract_identity",
        ),
        sa.UniqueConstraint("symbol", name="uq_option_contract_symbol"),
    )
    op.create_index(
        "ix_option_contracts_instrument_expiry",
        "option_contracts",
        ["instrument_id", "expiry_date"],
    )

    op.create_table(
        "instrument_master_sync_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("instruments_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("contracts_added", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("contracts_expired", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("detail", sa.String(1000), nullable=False, server_default=""),
    )

    op.create_table(
        "quote_ticks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            nullable=True,
        ),
        sa.Column(
            "option_contract_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("option_contracts.id"),
            nullable=True,
        ),
        sa.Column("ltp", sa.Numeric(12, 4), nullable=False),
        sa.Column("bid", sa.Numeric(12, 4), nullable=False),
        sa.Column("ask", sa.Numeric(12, 4), nullable=False),
        sa.Column("volume", sa.Integer(), nullable=False),
        sa.Column("oi", sa.Integer(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(instrument_id IS NOT NULL) <> (option_contract_id IS NOT NULL)",
            name="ck_quote_tick_exactly_one_reference",
        ),
    )
    op.create_index("ix_quote_ticks_instrument_ts", "quote_ticks", ["instrument_id", "ts"])
    op.create_index(
        "ix_quote_ticks_option_contract_ts", "quote_ticks", ["option_contract_id", "ts"]
    )

    op.create_table(
        "depth_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "option_contract_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("option_contracts.id"),
            nullable=False,
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bid_levels", postgresql.JSONB(), nullable=False),
        sa.Column("ask_levels", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_depth_snapshots_option_contract_ts", "depth_snapshots", ["option_contract_id", "ts"]
    )

    op.create_table(
        "option_chain_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            nullable=False,
        ),
        sa.Column("expiry_date", sa.Date(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chain_data", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        "ix_option_chain_snapshots_instrument_expiry_ts",
        "option_chain_snapshots",
        ["instrument_id", "expiry_date", "ts"],
    )

    op.create_table(
        "indicator_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            nullable=True,
        ),
        sa.Column(
            "option_contract_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("option_contracts.id"),
            nullable=True,
        ),
        sa.Column("indicator_name", sa.String(30), nullable=False),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("value", sa.Numeric(14, 4), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(instrument_id IS NOT NULL) <> (option_contract_id IS NOT NULL)",
            name="ck_indicator_snapshot_exactly_one_reference",
        ),
    )
    op.create_index(
        "ix_indicator_snapshots_instrument",
        "indicator_snapshots",
        ["instrument_id", "indicator_name", "timeframe", "ts"],
    )
    op.create_index(
        "ix_indicator_snapshots_option_contract",
        "indicator_snapshots",
        ["option_contract_id", "indicator_name", "timeframe", "ts"],
    )


def downgrade() -> None:
    op.drop_table("indicator_snapshots")
    op.drop_table("option_chain_snapshots")
    op.drop_table("depth_snapshots")
    op.drop_table("quote_ticks")
    op.drop_table("instrument_master_sync_log")
    op.drop_table("option_contracts")
    op.drop_table("instruments")
