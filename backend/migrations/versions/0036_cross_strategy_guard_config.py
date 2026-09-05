"""risk_limit_configs.cross_strategy_guard_enabled

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-05

Kill switch for Guard 1 (cross-strategy same-direction "trouble" lock):
blocks a new options-buying entry when 2+ other positions (any strategy,
any strike) on the same underlying + option_type are currently <= -Rs300
P&L per lot, either open (live unrealized P&L) or closed within the last
10 minutes (realized P&L). Validated via a chronological counterfactual
simulation against real OCI Postgres trade data across 7 real trading
days (net +Rs14,696.50). The three validated numbers (Rs300/lot,
count-of-2, 10-min window) are deliberately hardcoded module constants in
risk_engine/service.py, not columns here -- same precedent as Guard 2's
own _REENTRY_COOLDOWN. Only the enable/disable flag is a config column,
so it can be turned off via a config version bump without a code deploy.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "risk_limit_configs",
        sa.Column(
            "cross_strategy_guard_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("risk_limit_configs", "cross_strategy_guard_enabled")
