"""backfill strategy_configs.runtime_mode=force_paper

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-17

Mode master-switch feature. Pure data migration, no schema change.

`runtime_mode` (added in migration 0014) has always defaulted to NULL,
meaning "no override -- defer to `status`'s own tier." Combined with
`SafeMode.LIVE_ENABLED` (which ignores `status` entirely and only checks
`runtime_mode != force_paper`), NULL has quietly meant "eligible for live
the moment the session mode allows it" since Phase 6 -- inert only because
nothing in the app could ever actually drive a session into `live_enabled`
before the master-switch feature added `set_master_trading_mode`/
`go-live`. Once that becomes reachable, every existing strategy with
`runtime_mode IS NULL` (i.e. all of them today, since force_paper was
never a widely-used per-strategy setting) would become live-eligible the
instant a human flips the new master switch to Live, regardless of
whether they'd looked at that particular strategy's own Mode dropdown --
a fail-open default for a real-money gate.

This sets every existing row to `force_paper` explicitly, so "Live" (an
explicit, per-strategy human choice via the relabeled Mode dropdown) is
strictly opt-in going forward. `create_strategy` (`api/v1/strategies.py`)
was changed the same day to default new rows to `force_paper` too, for
the same reason -- this migration only needs to cover rows that already
existed before that code change shipped.

Downgrade is a no-op: `runtime_mode`'s prior per-row state isn't recorded
anywhere to restore, same reasoning as migration 0018's own downgrade.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

strategy_configs_table = sa.table(
    "strategy_configs",
    sa.column("runtime_mode", sa.String),
)


def upgrade() -> None:
    op.execute(strategy_configs_table.update().values(runtime_mode="force_paper"))


def downgrade() -> None:
    pass
