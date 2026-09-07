"""stop_plans.exit_fired_at + exit_fire_attempts

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-07

Fire-now-exit state for the resting protective SL-LMT (2026-09-07 live
incident: the trail/EOD/manual exit path placed a fresh LIMIT sell that
Shoonya RMS margin-rejected as a naked short; the fix drives the already-
accepted SL-LMT instead -- `exit_via_resting_stop`).

- `exit_fired_at`: set the moment a fire-now `ModifyOrder` on the resting
  order is confirmed. The idempotency signal -- "repriced to fire, awaiting
  the async fill, do NOT re-modify every cycle". `None` = not yet fired.
- `exit_fire_attempts`: incremented on each *failed* fire-now modify. A
  position with a resting order never creates `exit:{id}` order rows, so
  the existing Order-count exhaustion check in `close_position` can't see
  it; this counter drives the same `_MAX_EXIT_ORDER_ATTEMPTS` -> exhaustion
  escalation for that path.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stop_plans",
        sa.Column("exit_fired_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "stop_plans",
        sa.Column(
            "exit_fire_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("stop_plans", "exit_fire_attempts")
    op.drop_column("stop_plans", "exit_fired_at")
