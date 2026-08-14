"""backfill strategy_configs.underlying_symbol/is_enabled

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-14

Ops-Hardening Phase 7. Pure data migration, no schema change -- sets
underlying_symbol='NIFTY' and is_enabled=true on every existing
strategy_configs row, so the Phase 6 auto-spawner has something real to
spawn out-of-the-box instead of alerting "no underlying_symbol configured"
for every pre-existing strategy. A no-op against a genuinely fresh database
(underlying_symbol was only added in migration 0016, so every row is
already NULL; is_enabled already defaults true from migration 0014) --
written unconditionally anyway as an explicit guarantee for whatever a real
dev/staging database actually has, per explicit user request. Deliberately
does not touch `status` (the graduation ladder) or `strategy_type`/`params`.

Downgrade is a no-op: there is no prior underlying_symbol value to restore
(every row was NULL before this ran), and is_enabled's prior per-row state
isn't recorded anywhere to restore either.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

strategy_configs_table = sa.table(
    "strategy_configs",
    sa.column("underlying_symbol", sa.String),
    sa.column("is_enabled", sa.Boolean),
)


def upgrade() -> None:
    op.execute(
        strategy_configs_table.update().values(underlying_symbol="NIFTY", is_enabled=True)
    )


def downgrade() -> None:
    pass
