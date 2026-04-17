"""Fixtures pytest partagées."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from polycopy.storage.engine import create_engine_and_session
from polycopy.storage.models import Base
from polycopy.storage.repositories import (
    DetectedTradeRepository,
    StrategyDecisionRepository,
    TargetTraderRepository,
)

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest_asyncio.fixture
async def async_engine() -> AsyncIterator[AsyncEngine]:
    engine, _ = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(
    async_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def target_trader_repo(
    session_factory: async_sessionmaker[AsyncSession],
) -> TargetTraderRepository:
    return TargetTraderRepository(session_factory)


@pytest_asyncio.fixture
async def detected_trade_repo(
    session_factory: async_sessionmaker[AsyncSession],
) -> DetectedTradeRepository:
    return DetectedTradeRepository(session_factory)


@pytest_asyncio.fixture
async def strategy_decision_repo(
    session_factory: async_sessionmaker[AsyncSession],
) -> StrategyDecisionRepository:
    return StrategyDecisionRepository(session_factory)


@pytest.fixture
def sample_activity_payload() -> list[dict[str, Any]]:
    """Réponse réelle Data API capturée pour les tests unitaires."""
    return list(json.loads((_FIXTURES_DIR / "activity_sample.json").read_text()))


@pytest.fixture
def sample_gamma_market() -> dict[str, Any]:
    """1er marché de la fixture Gamma /markets capturée."""
    payload = json.loads((_FIXTURES_DIR / "gamma_market_sample.json").read_text())
    first: dict[str, Any] = payload[0]
    return first


@pytest.fixture
def sample_clob_midpoint() -> dict[str, str]:
    """Réponse CLOB /midpoint capturée."""
    payload: dict[str, str] = json.loads(
        (_FIXTURES_DIR / "clob_midpoint_sample.json").read_text(),
    )
    return payload
