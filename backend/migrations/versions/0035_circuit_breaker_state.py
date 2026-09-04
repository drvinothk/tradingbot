"""strategy_runs circuit-breaker state + strategy_configs.runtime_mode_source

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-04

Issue 5 (docs/ops/reliability_fixes_plan_2026_09_04.md): replaces the
session-wide, win-gated `consecutive_loss_pause_active` risk check (a
structural deadlock -- new entries are blocked while paused, so there's no
path back to the win that would reset it) with a per-strategy, time-bounded
auto live<->paper circuit breaker.

`strategy_runs` gets the breaker's own state (resets naturally each trading
day, since a new run starts each day -- no explicit daily-reset step
needed); `strategy_configs` gets `runtime_mode_source` so the breaker's own
auto-flips can be told apart from a human's deliberate `runtime_mode` edits.
All four columns nullable/defaulted, no backfill needed -- every existing
row reads as "never tripped" / "not auto-managed", the correct default.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "strategy_runs",
        sa.Column("consecutive_severe_losses", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "strategy_runs",
        sa.Column("cooldown_tier", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "strategy_runs",
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "strategy_configs",
        sa.Column("runtime_mode_source", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategy_configs", "runtime_mode_source")
    op.drop_column("strategy_runs", "cooldown_until")
    op.drop_column("strategy_runs", "cooldown_tier")
    op.drop_column("strategy_runs", "consecutive_severe_losses")
