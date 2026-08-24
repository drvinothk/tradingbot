"""structure-break confirmation: ATR buffer + persistence fields on
signals/trade_intents/stop_plans

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-24

Fixes the confirmed structure_break noise-sensitivity bug (raw-tick, zero-
debounce comparison against `stop_plan.structure_level` — see project memory
`project_structure_break_noise_bug_2026_08_21` /
`project_structure_break_confirmation_fix_plan_2026_08_24`).

`structure_break_buffer` / `structure_break_persistence_seconds` (signals,
trade_intents, stop_plans): frozen at signal time, same pattern
`structure_level` itself already uses (migration 0008) -- an ATR-scaled
minimum-breach margin and a minimum-persistence window before a structure
breach counts as confirmed, rather than firing on a single noisy tick.
Nullable, no backfill -- null on either means "no buffer / confirm
immediately", i.e. today's exact existing behavior, so this is safe with
positions already open mid-migration.

`structure_break_candidate_since` / `structure_break_candidate_extreme`
(stop_plans only): mutable, updated live by `evaluate_open_position` while a
breach is in its unconfirmed candidate window -- `candidate_since` is cleared
back to null the moment price reclaims the level.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("signals", "trade_intents"):
        op.add_column(
            table,
            sa.Column("structure_break_buffer", sa.Numeric(precision=12, scale=4), nullable=True),
        )
        op.add_column(
            table,
            sa.Column(
                "structure_break_persistence_seconds", sa.Numeric(precision=6, scale=2), nullable=True
            ),
        )

    op.add_column(
        "stop_plans",
        sa.Column("structure_break_buffer", sa.Numeric(precision=12, scale=4), nullable=True),
    )
    op.add_column(
        "stop_plans",
        sa.Column(
            "structure_break_persistence_seconds", sa.Numeric(precision=6, scale=2), nullable=True
        ),
    )
    op.add_column(
        "stop_plans",
        sa.Column("structure_break_candidate_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "stop_plans",
        sa.Column(
            "structure_break_candidate_extreme", sa.Numeric(precision=12, scale=4), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("stop_plans", "structure_break_candidate_extreme")
    op.drop_column("stop_plans", "structure_break_candidate_since")
    op.drop_column("stop_plans", "structure_break_persistence_seconds")
    op.drop_column("stop_plans", "structure_break_buffer")

    for table in ("trade_intents", "signals"):
        op.drop_column(table, "structure_break_persistence_seconds")
        op.drop_column(table, "structure_break_buffer")
