"""Bootstrap du schéma DB via Alembic et upsert initial des wallets cibles.

Depuis M4, le schéma est géré par Alembic (``alembic upgrade head``). Deux cas :

* DB vierge → ``alembic upgrade head`` applique la baseline puis les migrations.
* DB héritée de M3 (tables déjà présentes, pas de ``alembic_version``) → on
  ``stamp`` la baseline avant ``upgrade head`` pour préserver les données.

Voir ``specs/M4-monitoring.md`` §7.5 et ``docs/setup.md`` §10.
"""

import asyncio
from pathlib import Path

import structlog
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alembic import command
from polycopy.storage.models import Base
from polycopy.storage.repositories import TargetTraderRepository

log = structlog.get_logger(__name__)

_BASELINE_REVISION = "0001_baseline_m3"
_ALEMBIC_INI_FILENAME = "alembic.ini"


def _project_root() -> Path:
    """Racine du repo (là où vit ``alembic.ini``)."""
    return Path(__file__).resolve().parents[3]


def _sync_db_url(url: str) -> str:
    """Convertit une URL async SQLAlchemy en URL sync pour Alembic."""
    for async_prefix, sync_prefix in {
        "sqlite+aiosqlite://": "sqlite://",
        "postgresql+asyncpg://": "postgresql://",
    }.items():
        if url.startswith(async_prefix):
            return sync_prefix + url[len(async_prefix) :]
    return url


def _run_alembic_upgrade(database_url: str) -> None:
    """Applique les migrations Alembic.

    Détecte le cas "DB M3 préexistante" (tables présentes mais pas de
    ``alembic_version``) et stamp la baseline avant d'appliquer les deltas.
    """
    from sqlalchemy import create_engine

    sync_url = _sync_db_url(database_url)
    cfg = Config(str(_project_root() / _ALEMBIC_INI_FILENAME))
    cfg.set_main_option("sqlalchemy.url", sync_url)

    engine = create_engine(sync_url)
    try:
        with engine.connect() as conn:
            inspector = inspect(conn)
            existing_tables = set(inspector.get_table_names())
    finally:
        engine.dispose()

    has_alembic_version = "alembic_version" in existing_tables
    has_legacy_tables = bool(existing_tables - {"alembic_version"})

    if has_legacy_tables and not has_alembic_version:
        log.info("alembic_stamping_baseline", revision=_BASELINE_REVISION)
        command.stamp(cfg, _BASELINE_REVISION)
    command.upgrade(cfg, "head")


async def init_db(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    target_wallets: list[str],
) -> None:
    """Applique les migrations Alembic puis upsert chaque wallet cible."""
    database_url = str(engine.url.render_as_string(hide_password=False))
    await asyncio.to_thread(_run_alembic_upgrade, database_url)

    repo = TargetTraderRepository(session_factory)
    for wallet in target_wallets:
        await repo.upsert(wallet)
    log.info(
        "db_initialized",
        tables=sorted(Base.metadata.tables.keys()),
        targets_count=len(target_wallets),
    )
