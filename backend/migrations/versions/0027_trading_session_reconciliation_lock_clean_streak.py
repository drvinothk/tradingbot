"""trading_sessions.reconciliation_lock_clean_streak

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-25

Reconciliation-lock unattended auto-recovery feature. Tracks consecutive
clean `run_full_reconciliation` checks while a session is in
RECONCILIATION_LOCK -- driven only by the new
`ReconciliationLockRecoveryScheduler`, reset to 0 on every fresh entry into
the lock (see `app.core.modes.state_machine._write_transition`) and on any
dirty check. Reaching the scheduler's own threshold auto-recovers the
session, including back to a live `prior_mode` -- see
`state_machine.recover_from_reconciliation_lock`'s own docstring for the
full design (a deliberate, scoped exception to this codebase's Rule 4).
Same shape as migration 0005's `consecutive_losses` addition right next to
it in the model.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trading_sessions",
        sa.Column(
            "reconciliation_lock_clean_streak", sa.Integer(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("trading_sessions", "reconciliation_lock_clean_streak")
