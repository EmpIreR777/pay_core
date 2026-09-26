from alembic import context
from sqlalchemy import engine_from_config, pool, text
from src.core.config import settings
from src.db.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    context.configure(
        url=settings.SQLALCHEMY_SYNC_DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
        version_table_schema='files',
        include_schemas=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        url=settings.SQLALCHEMY_SYNC_DB_URL,
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        connection.execute(text('CREATE SCHEMA IF NOT EXISTS files'))
        connection.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema='files',
            include_schemas=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
