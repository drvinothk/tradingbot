"""stop_plans.suppress_hard_target

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-08

When a staged-exit position collapses to a single full-qty exit (1 lot, or a
fill that isn't a whole-lot multiple), the single `StopPlan`/`TrailPlan` is now
sourced from the highest-`qty_fraction` leg spec rather than the top-level
`params` (2026-09-08 -- `exit_legs.pick_collapsed_exit_leg`). If that dominant
leg is a no-target ("runner") leg (`ExitLegSpec.target_price is None`), the
collapsed single exit must also have no hard target -- but the legacy
single-exit path in `evaluate_open_position` reads `trade_intent.target_price`
(NOT NULL) directly and has no no-target concept. This nullable flag tells that
step-2 check to skip.

`None` (every existing row, every non-collapsed position) == not suppressed ==
today's behaviour, so this column is invisible to every existing reader. Pure
metadata add (nullable, no default) -- no table rewrite.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stop_plans",
        sa.Column("suppress_hard_target", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stop_plans", "suppress_hard_target")
