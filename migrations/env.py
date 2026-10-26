"""Alembic environment for the Phase 3 SQLite schema."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from venaroq.adapters.persistence.sqlite.connection import (
    apply_sync_connection_policy,
    prepare_database_file,
)
from venaroq.config.database import DatabaseSettings

config = context.config
# Callers that already own logging (the test suite) opt out; fileConfig would otherwise replace the
# root handlers and disable every logger created before it.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

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
