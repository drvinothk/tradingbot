"""schema drift cleanup: session/user unique-index consolidation,
drop stale indexes superseded by Phase 2/3 model changes

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-26

Found while generating Phase 4's migration: `migrations/env.py` only ever
imported 4 of the 9 domain packages (`audit, identity, market, session` —
missing `broker, execution, ops, risk, strategy`, all added by Phase 2/3),
so every autogenerate run since Phase 2 compared the live DB against an
incomplete model set. Once that import list was fixed (see env.py), a real
but unrelated drift showed up: the DB still carries pre-Phase-2 index/
constraint shapes that current models no longer match, never migrated:

- `sessions.token_hash` / `users.email`: the model declares
  `unique=True, index=True` on the column (one unique index); the DB still
  has the *old* separately-named unique constraint (`sessions_token_hash_key`
  / `users_email_key`) plus a *separately-named*, non-unique index
  (`ix_sessions_token_hash` / `ix_users_email`) from before the model
  consolidated to one. Uniqueness was never actually missing — this just
  reconciles the DB's DDL shape with what the current model produces.
- `trading_sessions.broker_account_id`: the model no longer declares a
  standalone index here (superseded by `user_broker_access`'s own
  `uq_user_broker_account` unique constraint, already in place since
  migration 0001).
- `session_mode_transitions`: the model no longer declares
  `ix_session_mode_transitions_session`.

No Phase 4 code depends on this; split out so the Phase 4 migration only
contains Phase 4's own schema changes.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_session_mode_transitions_session", table_name="session_mode_transitions")

    op.drop_constraint("sessions_token_hash_key", "sessions", type_="unique")
    op.drop_index("ix_sessions_token_hash", table_name="sessions")
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=True)

    op.drop_index("ix_trading_sessions_broker_account", table_name="trading_sessions")

    op.drop_constraint("users_email_key", "users", type_="unique")
    op.drop_index("ix_users_email", table_name="users")
    op.create_index("ix_users_email", "users", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_email", table_name="users")
    op.create_index("ix_users_email", "users", ["email"], unique=False)
    op.create_unique_constraint("users_email_key", "users", ["email"])

    op.create_index(
        "ix_trading_sessions_broker_account", "trading_sessions", ["broker_account_id"], unique=False
    )

    op.drop_index("ix_sessions_token_hash", table_name="sessions")
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=False)
    op.create_unique_constraint("sessions_token_hash_key", "sessions", ["token_hash"])

    op.create_index(
        "ix_session_mode_transitions_session", "session_mode_transitions", ["trading_session_id"], unique=False
    )
