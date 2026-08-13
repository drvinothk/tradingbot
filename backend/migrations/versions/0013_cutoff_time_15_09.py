"""cutoff_time default 15:20 -> 15:09

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-13

Consolidates the EOD force-square-off boundary with the new 09:31-15:09 IST
trade-firing window (`app.modules.strategy_engine.runner.TRADE_WINDOW_END`)
so a position can no longer be opened in the last minutes of the day and
immediately force-closed. DB-level `server_default` only, matching how
`0003_sessions_and_audit.py` originally set it — the ORM-side `default=` on
`TradingSession.cutoff_time` (`app/domain/session/models.py`) is updated
separately in the same commit. Existing rows are untouched by this
migration (a `server_default` change only affects future inserts); any
already-live `TradingSession` row that should also move to 15:09 needs its
own explicit, scoped `UPDATE`, done separately and deliberately, not as
part of a schema migration.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "trading_sessions",
        "cutoff_time",
        server_default="15:09:00",
    )


def downgrade() -> None:
    op.alter_column(
        "trading_sessions",
        "cutoff_time",
        server_default="15:20:00",
    )
