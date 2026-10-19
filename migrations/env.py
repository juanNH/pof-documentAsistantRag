from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from rag_assistant.database import Base, database_url
import rag_assistant.db_models  # noqa: F401 - register ORM tables in metadata


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

configured_url = database_url()
if configured_url:
    rendered_url = configured_url.render_as_string(hide_password=False) if hasattr(configured_url, "render_as_string") else configured_url
    config.set_main_option("sqlalchemy.url", rendered_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
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
