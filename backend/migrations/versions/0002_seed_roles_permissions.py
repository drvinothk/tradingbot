"""seed starter roles and permissions

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-24

Pure reference data (roles/permissions), safe to bake into the migration
history itself — unlike the default workspace/admin user, which involves a
real secret and is created separately via scripts/bootstrap_admin.py.
"""

from __future__ import annotations

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.domain.identity.seed_data import PERMISSIONS, ROLES

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

roles_table = sa.table(
    "roles",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("name", sa.String),
)
permissions_table = sa.table(
    "permissions",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("code", sa.String),
    sa.column("description", sa.String),
)
role_permissions_table = sa.table(
    "role_permissions",
    sa.column("role_id", postgresql.UUID(as_uuid=True)),
    sa.column("permission_id", postgresql.UUID(as_uuid=True)),
)


def upgrade() -> None:
    permission_ids = {code: uuid.uuid4() for code in PERMISSIONS}
    role_ids = {name: uuid.uuid4() for name in ROLES}

    op.bulk_insert(
        permissions_table,
        [
            {"id": permission_ids[code], "code": code, "description": description}
            for code, description in PERMISSIONS.items()
        ],
    )
    op.bulk_insert(
        roles_table,
        [{"id": role_ids[name], "name": name} for name in ROLES],
    )
    op.bulk_insert(
        role_permissions_table,
        [
            {"role_id": role_ids[role_name], "permission_id": permission_ids[code]}
            for role_name, codes in ROLES.items()
            for code in codes
        ],
    )


def downgrade() -> None:
    op.execute(role_permissions_table.delete())
    op.execute(roles_table.delete())
    op.execute(permissions_table.delete())
