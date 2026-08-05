"""strategy_runs.instrument_id/expiry_date/interval_seconds

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-05

Nullable, no backfill: these were always request params to POST
/strategies/{id}/start, never persisted anywhere (the only place a run's
instrument/expiry lived was the in-memory Strategy object inside its runner
thread), so existing rows have no source to backfill from. A resume-on-
startup pass must skip any row where instrument_id is still NULL — those
predate this column and can't be resumed, only restarted fresh via the API.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "strategy_runs", sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("strategy_runs", sa.Column("expiry_date", sa.Date(), nullable=True))
    op.add_column("strategy_runs", sa.Column("interval_seconds", sa.Float(), nullable=True))
    op.create_foreign_key(
        "fk_strategy_runs_instrument_id",
        "strategy_runs",
        "instruments",
        ["instrument_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_strategy_runs_instrument_id", "strategy_runs", type_="foreignkey")
    op.drop_column("strategy_runs", "interval_seconds")
    op.drop_column("strategy_runs", "expiry_date")
    op.drop_column("strategy_runs", "instrument_id")
