"""instruments.freeze_qty

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-03

Guardrail-layer batch: NSE F&O "freeze quantity" (exchange-imposed max order
size). Nullable, operator-supplied on purpose (see domain/market/models.py's
Instrument.freeze_qty docstring) - no default-changing step needed, every
existing row stays NULL (check no-op) until explicitly populated.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("instruments", sa.Column("freeze_qty", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("instruments", "freeze_qty")
