"""Bootstrap du schéma DB et upsert initial des wallets cibles.

TODO M2+: introduire Alembic pour la gestion des migrations en place de
`Base.metadata.create_all`.
"""

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from polycopy.storage.models import Base
from polycopy.storage.repositories import TargetTraderRepository

log = structlog.get_logger(__name__)


async def init_db(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    target_wallets: list[str],
) -> None:
    """Crée les tables si absentes et upsert chaque wallet cible (idempotent)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    repo = TargetTraderRepository(session_factory)
    for wallet in target_wallets:
        await repo.upsert(wallet)
    log.info(
        "db_initialized",
        tables_created=sorted(Base.metadata.tables.keys()),
        targets_count=len(target_wallets),
    )
