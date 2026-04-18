"""Test du bootstrap DB (`init_db`).

À partir de M4 ``init_db`` lance Alembic (sync) sur la DB — donc on ne peut pas
utiliser ``sqlite:///:memory:`` qui n'est pas partagé entre connexions. Chaque
test se voit attribuer un fichier temporaire.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from polycopy.storage.init_db import init_db
from polycopy.storage.repositories import TargetTraderRepository


@pytest_asyncio.fixture
async def tempfile_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    db_path = tmp_path / "init_db_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_init_db_creates_tables_and_upserts_wallets(
    tempfile_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(tempfile_engine, expire_on_commit=False)
    wallets = ["0xAAA", "0xBBB", "0xAAA"]  # doublon volontaire
    await init_db(tempfile_engine, session_factory, wallets)

    repo = TargetTraderRepository(session_factory)
    actives = await repo.list_active()
    assert {t.wallet_address for t in actives} == {"0xaaa", "0xbbb"}

    # idempotence : second appel ne crée pas de doublons
    await init_db(tempfile_engine, session_factory, wallets)
    actives_again = await repo.list_active()
    assert len(actives_again) == 2
