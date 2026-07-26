from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config.settings import get_settings
from app.core.db.base import Base

# Import every domain package here so its models register on Base.metadata
# before autogenerate runs. Add new domain packages as Phase 1+ introduces them.
# (Phase 2/3 added broker/execution/ops/risk/strategy without updating this
# list — caught during Phase 4 planning when autogenerate tried to drop
# every table those packages define; see the build plan's Phase 4 QC notes.)
from app.domain import (  # noqa: F401
    audit,
    broker,
    execution,
    identity,
    market,
    ops,
    risk,
    session,
    strategy,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().db.sqlalchemy_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
