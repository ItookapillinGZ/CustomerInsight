"""Alembic owns schema creation; application import never creates tables."""

from logging.config import fileConfig
import os

from alembic import context

from backend.database import Base, get_engine
# Register frozen model definitions for schema comparison, not schema mutation.
from backend import accounts, pg_history, pg_jobs  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def migration_url():
    value = os.getenv("APP_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not value:
        raise RuntimeError("APP_DATABASE_URL is required for migrations")
    return value


if context.is_offline_mode():
    context.configure(url=migration_url(), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"}, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied = config.attributes.get("connection")
    if supplied is not None:
        context.configure(connection=supplied, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    else:
        # Explicit test URLs may use SQLite; deployed env URLs are PostgreSQL.
        engine = get_engine() if os.getenv("APP_DATABASE_URL") else get_engine(migration_url())
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
