"""strategy_configs.is_enabled/runtime_mode

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-14

Ops-Hardening Phase 1. `is_enabled` (default true, backfills every existing
row to enabled — matches current behavior, where every config with a
`StrategyRun` was implicitly "on"): a master on/off switch independent of
`status`'s graduation ladder, load-bearing for the future daily bootstrapper
(Phase 4), which has no existing `StrategyRun` rows to resume for a
freshly-created session and needs some config-level signal for what to
auto-start. `runtime_mode` (nullable, no backfill — `None` means "no
override, defer to `status`"): a same-day tactical downgrade toggle, kept
deliberately separate from `status` itself rather than repurposing it, since
`status` already means something different (long-term graduation stage, not
a daily switch) — see `StrategyRuntimeMode`'s own docstring
(`app/domain/strategy/models.py`).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "strategy_configs",
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "strategy_configs",
        sa.Column("runtime_mode", sa.String(length=30), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategy_configs", "runtime_mode")
    op.drop_column("strategy_configs", "is_enabled")
