"""seed INDIA VIX instrument row

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-19

VIX/PCR environment-metrics feed. India VIX is never traded (no lot size,
no tick size in any real sense), but reuses the existing `Instrument`/
`QuoteTick` ingestion pipeline exactly as-is -- `lot_size`/`tick_size` are
NOT NULL columns on `instruments`, so placeholder values are seeded here
rather than making those columns nullable just for this one row. Symbol
matches `market_data.market_hours.ENV_METRIC_SYMBOLS` and both providers'
own symbol-translation maps (`truedata_provider._TO_TRUEDATA_SYMBOL`,
`shoonya.adapter._UNDERLYING_INDEX_TSYM`) exactly -- all three must agree
on this string or ingestion silently subscribes to nothing.

Idempotent (`ON CONFLICT DO NOTHING` on the existing `uq_instrument_symbol_
exchange` constraint) -- safe to re-run, and safe if a prior manual seed
already created this row under the same (symbol, exchange).

Downgrade removes only this exact row, by symbol+exchange, not a blanket
delete -- never touches a real instrument that happens to share the id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

instruments_table = sa.table(
    "instruments",
    sa.column("id", sa.dialects.postgresql.UUID(as_uuid=True)),
    sa.column("symbol", sa.String),
    sa.column("exchange", sa.String),
    sa.column("lot_size", sa.Integer),
    sa.column("tick_size", sa.Numeric),
    sa.column("is_active", sa.Boolean),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)

SYMBOL = "INDIA VIX"
EXCHANGE = "NSE"


def upgrade() -> None:
    now = datetime.now(UTC)
    op.execute(
        sa.dialects.postgresql.insert(instruments_table)
        .values(
            id=uuid.uuid4(),
            symbol=SYMBOL,
            exchange=EXCHANGE,
            lot_size=1,
            tick_size=0.05,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_instrument_symbol_exchange")
    )


def downgrade() -> None:
    op.execute(
        instruments_table.delete().where(
            instruments_table.c.symbol == SYMBOL, instruments_table.c.exchange == EXCHANGE
        )
    )
