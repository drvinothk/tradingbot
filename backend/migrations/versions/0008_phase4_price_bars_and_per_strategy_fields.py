"""phase4: price_bars, strategy_configs.strategy_type, per-method trailing
and structure_level on signals/trade_intents/stop_plans

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-26

Phase 4 — ORB/VWAP Pullback/EMA Micro-pullback:

- `price_bars`: completed underlying OHLCV candles, populated from the `Bar`
  object `IndicatorEngine.on_tick` already built internally to feed EMA and
  previously discarded (see app/domain/market/models.py's module docstring).
- `strategy_configs.strategy_type`: which Strategy subclass
  api.v1.strategies.start_strategy instantiates. `server_default='synthetic'`
  so existing rows (Phase 2's synthetic strategy) don't need a backfill; the
  server default is dropped immediately after so future rows rely on the ORM
  model's default, not a stale DB default.
- `trail_activation_fraction` / `trail_lock_fraction` (signals, trade_intents):
  per-strategy override of the generic Phase-3 0.5/0.5 trailing rule in
  execution_engine/paper/service.py. Nullable — null means "use the generic
  default", so SyntheticStrategy's behavior is unchanged.
- `structure_level` (signals, trade_intents, stop_plans): the underlying-
  index structural invalidation level (opening-range boundary / pullback
  extreme / EMA9 value) that backs the new spread/structure-break exit path
  in evaluate_open_position — independent of stop_price/target_price, which
  are on the option premium, not the underlying.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "price_bars",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            nullable=False,
        ),
        sa.Column("timeframe", sa.String(length=10), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("high", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("low", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("close", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("volume", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "instrument_id", "timeframe", "bucket_start", name="uq_price_bar_bucket"
        ),
    )
    op.create_index(
        "ix_price_bars_instrument_timeframe_bucket",
        "price_bars",
        ["instrument_id", "timeframe", "bucket_start"],
    )

    op.add_column(
        "strategy_configs",
        sa.Column("strategy_type", sa.String(length=40), nullable=False, server_default="synthetic"),
    )
    op.alter_column("strategy_configs", "strategy_type", server_default=None)

    for table in ("signals", "trade_intents"):
        op.add_column(
            table, sa.Column("trail_activation_fraction", sa.Numeric(precision=6, scale=4), nullable=True)
        )
        op.add_column(
            table, sa.Column("trail_lock_fraction", sa.Numeric(precision=6, scale=4), nullable=True)
        )
        op.add_column(
            table, sa.Column("structure_level", sa.Numeric(precision=12, scale=4), nullable=True)
        )

    op.add_column(
        "stop_plans", sa.Column("structure_level", sa.Numeric(precision=12, scale=4), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("stop_plans", "structure_level")

    for table in ("trade_intents", "signals"):
        op.drop_column(table, "structure_level")
        op.drop_column(table, "trail_lock_fraction")
        op.drop_column(table, "trail_activation_fraction")

    op.drop_column("strategy_configs", "strategy_type")

    op.drop_index("ix_price_bars_instrument_timeframe_bucket", table_name="price_bars")
    op.drop_table("price_bars")
