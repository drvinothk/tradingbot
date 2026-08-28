"""Retire StrategyStatus + SafeMode.PAPER_PLUS_GUARDED_LIVE

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-28

The per-strategy graduation ladder is gone (see
`app.domain.strategy.models` / `app.domain.session.models` docstrings):

- `strategy_configs.status` (`StrategyStatus`) had no API setter and only
  one remaining reader (`is_strategy_routed_live`'s guarded-mode branch,
  also removed) — dropped outright.
- `SafeMode.PAPER_PLUS_GUARDED_LIVE` was only ever an intermediate hop the
  master switch walked straight through; `paper_only <-> live_enabled` is
  now a direct edge. Any live session sitting in the guarded mode is
  backfilled *down* to `paper_only` (safety-decreasing, so unattended-safe).
  Same for a `prior_mode` remembered for degraded/reconciliation-lock
  recovery.

Historical `session_mode_transitions` / `audit_events` rows keep their
`"paper_plus_guarded_live"` strings verbatim — those columns are plain
`String`/JSONB, never coerced back into the `SafeMode` enum at read time,
so a retired value there is inert audit history, not a runtime hazard.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE trading_sessions SET mode = 'paper_only' "
        "WHERE mode = 'paper_plus_guarded_live'"
    )
    op.execute(
        "UPDATE trading_sessions SET prior_mode = 'paper_only' "
        "WHERE prior_mode = 'paper_plus_guarded_live'"
    )
    op.drop_column("strategy_configs", "status")


def downgrade() -> None:
    # The mode/prior_mode backfill is intentionally not reversed — it was
    # safety-decreasing and 'paper_plus_guarded_live' is no longer a valid
    # SafeMode value to reintroduce.
    op.add_column(
        "strategy_configs",
        sa.Column(
            "status",
            sa.String(length=30),
            nullable=False,
            server_default="research",
        ),
    )
