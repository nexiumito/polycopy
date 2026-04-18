"""Configuration Alembic pour polycopy.

Pattern sync : on lit ``DATABASE_URL`` depuis ``polycopy.config.Settings`` puis
on convertit l'URL async (``sqlite+aiosqlite://``) en URL sync (``sqlite://``)
car ``command.upgrade`` est sync de toute façon. Voir ``specs/M4-monitoring.md``
§7.3 et ``docs/setup.md`` §10.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from polycopy.config import settings
from polycopy.storage.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _sync_db_url(url: str) -> str:
    """Convertit une URL SQLAlchemy async en URL sync pour Alembic.

    ``sqlite+aiosqlite:///x.db`` → ``sqlite:///x.db``
    ``postgresql+asyncpg://...`` → ``postgresql://...`` (pour futur Postgres).
    """
    replacements = {
        "sqlite+aiosqlite://": "sqlite://",
        "postgresql+asyncpg://": "postgresql://",
    }
    for async_prefix, sync_prefix in replacements.items():
        if url.startswith(async_prefix):
            return sync_prefix + url[len(async_prefix) :]
    return url


# URL sync : CLI (`alembic upgrade head`) passe par ici.
# Si l'utilisateur a déjà set `-x sqlalchemy.url=...` en ligne de commande ou
# via `cfg.set_main_option(...)` avant l'appel programmatique, on respecte.
_configured_url = config.get_main_option("sqlalchemy.url")
if not _configured_url:
    _configured_url = _sync_db_url(settings.database_url)
    config.set_main_option("sqlalchemy.url", _configured_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Mode offline : génère du SQL brut sans connexion DB."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Mode online : applique les migrations via un engine sync."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
