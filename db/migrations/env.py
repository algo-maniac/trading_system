import sys
from pathlib import Path

# Make `trading_system` importable so we can read settings for the DB URL
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from alembic import context
from sqlalchemy import create_engine, pool
from trading_system.config import get_settings

config = context.config

settings = get_settings()
# Force SQLAlchemy to use psycopg3 (the project's driver), not psycopg2.
db_url = settings.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
config.set_main_option("sqlalchemy.url", db_url)


def run_migrations_offline() -> None:
    context.configure(
        url=db_url,
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(db_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
