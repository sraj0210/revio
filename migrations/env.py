"""Alembic environment for the Phase 3 SQLite schema."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from revio.adapters.persistence.sqlite.connection import (
    apply_sync_connection_policy,
    prepare_database_file,
)
from revio.config.database import DatabaseSettings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = DatabaseSettings()
prepare_database_file(settings)
config.set_main_option("sqlalchemy.url", f"sqlite:///{settings.database_path}")
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
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
        apply_sync_connection_policy(connection, settings)
        connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
