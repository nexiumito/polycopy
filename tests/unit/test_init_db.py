"""Test du bootstrap DB (`init_db`)."""

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from polycopy.storage.init_db import init_db
from polycopy.storage.repositories import TargetTraderRepository


@pytest_asyncio.fixture
async def empty_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_init_db_creates_tables_and_upserts_wallets(
    empty_engine: AsyncEngine,
) -> None:
    session_factory = async_sessionmaker(empty_engine, expire_on_commit=False)
    wallets = ["0xAAA", "0xBBB", "0xAAA"]  # doublon volontaire
    await init_db(empty_engine, session_factory, wallets)

    repo = TargetTraderRepository(session_factory)
    actives = await repo.list_active()
    assert {t.wallet_address for t in actives} == {"0xaaa", "0xbbb"}

    # idempotence : second appel ne crée pas de doublons
    await init_db(empty_engine, session_factory, wallets)
    actives_again = await repo.list_active()
    assert len(actives_again) == 2
