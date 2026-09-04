"""system_alerts.mode

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-04

Persists alerting.manager.send_alert's own `mode` param (previously used
only in-memory to decide the Telegram push, then discarded) so Control
Room's "Attention Required" card can apply the same mode!=PAPER suppression
Telegram already has. Nullable, no backfill -- every existing row predates
this and is treated as `None` ("not paper-suppressed"), the same safe
default send_alert's own `mode=None` case already means.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("system_alerts", sa.Column("mode", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("system_alerts", "mode")
