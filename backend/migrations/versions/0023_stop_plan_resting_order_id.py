"""stop_plans.resting_order_id

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-22

Bracket-order research concluded BO/CO aren't available for options on
Shoonya (see the build plan / project memory) -- this is the resulting
"Hard SL with Local Target" design instead: a LIVE-only crash-resilience
layer that places a real broker-side SL-LMT immediately on entry fill.
`resting_order_id` is the broker's order id for that position's currently-
resting protective stop, nullable, no parallel state enum (see
`StopPlan.resting_order_id`'s own docstring in app/domain/execution/models.py
for why). Nullable, no backfill needed -- every existing open position
predates this feature and simply has no resting order (correct: it falls
back to today's local-only monitoring until closed).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stop_plans",
        sa.Column("resting_order_id", sa.String(length=60), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stop_plans", "resting_order_id")
