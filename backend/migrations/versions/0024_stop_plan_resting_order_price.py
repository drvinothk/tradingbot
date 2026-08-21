"""stop_plans.resting_order_price

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-22

Second half of the "Hard SL with Local Target" TSL design (see 0023 and
`StopPlan.resting_order_price`'s own docstring in
app/domain/execution/models.py): tracks the trigger price last
*successfully confirmed* armed at the broker for `resting_order_id`, so a
failed `ModifyOrder` call (rejected, network error) is retried on every
later evaluate_open_position cycle rather than only on the next real trail
tightening -- the requested fallback for "what if a TSL modify is
rejected." Nullable, no backfill -- every existing StopPlan predates this
feature and has no resting order to track a price for.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stop_plans",
        sa.Column("resting_order_price", sa.Numeric(precision=12, scale=4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stop_plans", "resting_order_price")
