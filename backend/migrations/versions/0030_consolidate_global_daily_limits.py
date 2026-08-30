"""consolidate global_daily_limits_configs into session Daily Plan defaults

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-30

Drops `daily_max_lots` (superseded by a genuine per-strategy lot cap,
`StrategyConfig.params["qty_lots"]`, enforced in
`risk_engine.service.evaluate_trade_intent`) and adds
`daily_target_profit`/`daily_loss_cap`/`funding_mode` so this table now
mirrors `TradingSession`'s own Daily Plan fields 1:1 and can serve as their
default values -- see `GlobalDailyLimitsConfig`'s own docstring
(app.domain.ops.models) for the full design.

Existing row(s) backfilled with the same RiskDefaults-matching values the
model's own DEFAULT_* constants use, so a workspace that already had this
table seeded doesn't silently start defaulting a new session's target/loss
cap to 0.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "global_daily_limits_configs",
        sa.Column("daily_target_profit", sa.Numeric(14, 2), nullable=False, server_default="5000.00"),
    )
    op.add_column(
        "global_daily_limits_configs",
        sa.Column("daily_loss_cap", sa.Numeric(14, 2), nullable=False, server_default="5000.00"),
    )
    op.add_column(
        "global_daily_limits_configs",
        sa.Column("funding_mode", sa.String(10), nullable=False, server_default="cash"),
    )
    op.drop_column("global_daily_limits_configs", "daily_max_lots")


def downgrade() -> None:
    op.add_column(
        "global_daily_limits_configs",
        sa.Column("daily_max_lots", sa.Integer(), nullable=False, server_default="1"),
    )
    op.drop_column("global_daily_limits_configs", "funding_mode")
    op.drop_column("global_daily_limits_configs", "daily_loss_cap")
    op.drop_column("global_daily_limits_configs", "daily_target_profit")
