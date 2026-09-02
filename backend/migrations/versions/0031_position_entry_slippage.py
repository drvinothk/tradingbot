"""add positions.entry_slippage

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-02

Open-side counterpart of the already-existing TradeOutcome.slippage /
PositionExitLeg.slippage (exit-side, computed at close). Nullable and
additive-only -- no backfill for existing rows (the intended entry price
used to compute it isn't reliably reconstructable after the fact), no
change to any other column/constraint/index. See Position.entry_slippage's
own docstring (app.domain.execution.models) and
execution_engine.paper.service._open_position_from_fill for the
computation itself.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("entry_slippage", sa.Numeric(14, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("positions", "entry_slippage")
