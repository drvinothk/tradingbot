"""strategy_configs.runtime_mode -> authoritative, non-nullable

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-08

Paper/live model inversion. Before this, a real order needed the session
master switch (`SafeMode.LIVE_ENABLED`) *and* `runtime_mode != force_paper`;
`runtime_mode` had only `force_paper` (a brake) and `NULL` meant "follow the
session". The daily bootstrap always created `paper_only` sessions, so going
live was a manual, same-day human action.

After this, `runtime_mode` is the authoritative per-strategy real-money mark
(`force_live` / `force_paper`, non-nullable, default `force_paper`), the
daily session is born `live_enabled`, and the master switch is a global
paper clamp. `broker_adapter.composition.is_strategy_routed_live` now routes
live only for `runtime_mode == force_live` in a `live_enabled` session.

This migration does the data + constraint half:

- Every existing `runtime_mode IS NULL` row -> `force_paper`. Same
  fail-closed reasoning as migration 0019 (a null real-money gate must
  default to paper). Closes a known latent risk: the `Test 1` config
  carried `runtime_mode = NULL` and would have routed live on any
  master-switch flip.
- `SET DEFAULT 'force_paper'` then `SET NOT NULL`.

The new `is_strategy_routed_live` predicate already treats `NULL`/absent as
paper, so this migration is data hygiene + a constraint, not the safety
gate itself -- the predicate is. Deploy order is safe either way *except*
landing the born-live bootstrapper without the new predicate (see the plan).

Downgrade drops NOT NULL + the default. The pre-migration per-row NULL
state isn't recorded anywhere to restore, same as 0019's own no-op data
downgrade.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

strategy_configs_table = sa.table(
    "strategy_configs",
    sa.column("runtime_mode", sa.String),
)


def upgrade() -> None:
    op.execute(
        strategy_configs_table.update()
        .where(strategy_configs_table.c.runtime_mode.is_(None))
        .values(runtime_mode="force_paper")
    )
    op.alter_column(
        "strategy_configs",
        "runtime_mode",
        existing_type=sa.String(length=30),
        nullable=False,
        server_default="force_paper",
    )


def downgrade() -> None:
    op.alter_column(
        "strategy_configs",
        "runtime_mode",
        existing_type=sa.String(length=30),
        nullable=True,
        server_default=None,
    )
