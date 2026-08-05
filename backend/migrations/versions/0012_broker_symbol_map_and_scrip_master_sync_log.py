"""broker_symbol_map and scrip_master_sync_log

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-05

Additive only — no changes to `instruments`/`option_contracts`. Decoupling
market data (Angel One) from execution (Shoonya) needs a provider-keyed
symbol/token mapping for each *existing* Instrument/OptionContract row,
without changing what those rows' own `symbol` columns mean (see
`app.modules.market_data.scrip_master`'s own module docstring).

(Autogenerate also detected an unrelated, pre-existing `uq_user_broker_account`
unique-constraint drift on `user_broker_access` — left out of this revision
deliberately; that's a separate concern from this change and shouldn't be
bundled into an unrelated migration without its own review.)
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scrip_master_sync_log",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rows_parsed", sa.Integer(), nullable=False),
        sa.Column("rows_mapped", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.String(length=1000), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "broker_symbol_map",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("instrument_id", sa.UUID(), nullable=True),
        sa.Column("option_contract_id", sa.UUID(), nullable=True),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("external_symbol", sa.String(length=60), nullable=False),
        sa.Column("external_token", sa.String(length=40), nullable=False),
        sa.Column("exchange", sa.String(length=20), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(instrument_id IS NOT NULL) <> (option_contract_id IS NOT NULL)",
            name="ck_broker_symbol_map_exactly_one_of_instrument_or_contract",
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.ForeignKeyConstraint(["option_contract_id"], ["option_contracts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_id", "provider", name="uq_broker_symbol_map_instrument_provider"
        ),
        sa.UniqueConstraint(
            "option_contract_id",
            "provider",
            name="uq_broker_symbol_map_option_contract_provider",
        ),
    )
    op.create_index(
        "ix_broker_symbol_map_provider_token",
        "broker_symbol_map",
        ["provider", "external_token"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_broker_symbol_map_provider_token", table_name="broker_symbol_map")
    op.drop_table("broker_symbol_map")
    op.drop_table("scrip_master_sync_log")
