"""orders.intended_exit_reason

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-25

Closes a real, live-flagged mislabeling gap: `close_position` already knows
the real reason it's closing a position (STOP/TARGET/TRAIL/etc, passed in by
the caller), but a LIVE exit order that doesn't fill synchronously threw
that reason away -- `reconcile_pending_live_exit_orders` had no way to
recover it later and defaulted every late-discovered exit to the generic
`ExitReason.RECONCILED`, even when the real reason was a genuine TARGET hit
or TRAIL exit. `intended_exit_reason` is set by `close_position` at the
moment it places the `exit:{position_id}` order, before the async-fill
uncertainty window begins -- see `Order.intended_exit_reason`'s own
docstring in app/domain/execution/models.py. Nullable, no backfill: every
existing OPEN exit order predating this migration simply has no recorded
intent, which is exactly the honest `RECONCILED` fallback case.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("intended_exit_reason", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "intended_exit_reason")
