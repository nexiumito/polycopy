"""Tests TraderLifecycleFilter M5_bis Phase C.4.

Vérifie que le filtre bloque les BUY pour un wallet en sell_only /
blacklisted, laisse passer les SELL et les actives, et fast-path quand
EVICTION_ENABLED=false.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from polycopy.config import Settings
from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.models import Base
from polycopy.storage.repositories import TargetTraderRepository
from polycopy.strategy.dtos import PipelineContext
from polycopy.strategy.pipeline import TraderLifecycleFilter


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def target_repo(
    session_factory: async_sessionmaker[AsyncSession],
) -> TargetTraderRepository:
    return TargetTraderRepository(session_factory)


def _trade(wallet: str = "0xW", side: str = "BUY") -> DetectedTradeDTO:
    return DetectedTradeDTO(
        tx_hash="0xtx",
        target_wallet=wallet,
        condition_id="0xc",
        asset_id="123",
        side=side,  # type: ignore[arg-type]
        size=10.0,
        usdc_size=5.0,
        price=0.5,
        timestamp=datetime.now(tz=UTC),
        outcome="YES",
        slug=None,
        raw_json={},
    )


def _settings(*, eviction_enabled: bool = True) -> Settings:
    return Settings(eviction_enabled=eviction_enabled)


async def test_fast_path_when_eviction_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """EVICTION_ENABLED=false : pass-through sans query DB."""
    f = TraderLifecycleFilter(session_factory, _settings(eviction_enabled=False))
    ctx = PipelineContext(trade=_trade())
    result = await f.check(ctx)
    assert result.passed is True
    assert result.reason is None


async def test_sell_always_passes(
    session_factory: async_sessionmaker[AsyncSession],
    target_repo: TargetTraderRepository,
) -> None:
    """Side SELL : toujours autorisé (wind-down = on copie les SELL)."""
    await target_repo.insert_shadow("0xsell")
    await target_repo.transition_status("0xsell", new_status="active")
    await target_repo.transition_status("0xsell", new_status="sell_only")
    f = TraderLifecycleFilter(session_factory, _settings())
    ctx = PipelineContext(trade=_trade(wallet="0xsell", side="SELL"))
    result = await f.check(ctx)
    assert result.passed is True


async def test_buy_blocked_for_sell_only_wallet(
    session_factory: async_sessionmaker[AsyncSession],
    target_repo: TargetTraderRepository,
) -> None:
    """Side BUY + wallet sell_only → rejet avec reason='wallet_sell_only'."""
    await target_repo.insert_shadow("0xevicted")
    await target_repo.transition_status("0xevicted", new_status="active")
    await target_repo.transition_status("0xevicted", new_status="sell_only")
    f = TraderLifecycleFilter(session_factory, _settings())
    ctx = PipelineContext(trade=_trade(wallet="0xevicted", side="BUY"))
    result = await f.check(ctx)
    assert result.passed is False
    assert result.reason == "wallet_sell_only"


async def test_buy_blocked_for_blacklisted_wallet(
    session_factory: async_sessionmaker[AsyncSession],
    target_repo: TargetTraderRepository,
) -> None:
    """BUY + blacklisted → rejet (défense en profondeur ; le watcher
    normalement ne pollera pas un blacklisted, mais un race reste possible)."""
    await target_repo.insert_shadow("0xbl")
    await target_repo.transition_status_unsafe("0xbl", new_status="blacklisted")
    f = TraderLifecycleFilter(session_factory, _settings())
    ctx = PipelineContext(trade=_trade(wallet="0xbl", side="BUY"))
    result = await f.check(ctx)
    assert result.passed is False
    assert result.reason == "wallet_blacklisted"


async def test_buy_passes_for_active_wallet(
    session_factory: async_sessionmaker[AsyncSession],
    target_repo: TargetTraderRepository,
) -> None:
    """BUY + wallet active → pass."""
    await target_repo.insert_shadow("0xactive")
    await target_repo.transition_status("0xactive", new_status="active")
    f = TraderLifecycleFilter(session_factory, _settings())
    ctx = PipelineContext(trade=_trade(wallet="0xactive", side="BUY"))
    result = await f.check(ctx)
    assert result.passed is True


async def test_buy_passes_for_pinned_wallet(
    session_factory: async_sessionmaker[AsyncSession],
    target_repo: TargetTraderRepository,
) -> None:
    """BUY + pinned → pass (whitelist user absolue)."""
    await target_repo.upsert("0xpin")
    f = TraderLifecycleFilter(session_factory, _settings())
    ctx = PipelineContext(trade=_trade(wallet="0xpin", side="BUY"))
    result = await f.check(ctx)
    assert result.passed is True


async def test_buy_passes_for_unknown_wallet_defensive(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """BUY + wallet inconnu en DB → pass (défense : le M5 pipeline gère
    l'enregistrement après détection ; on ne bloque pas un wallet qu'on ne
    connaît pas encore)."""
    f = TraderLifecycleFilter(session_factory, _settings())
    ctx = PipelineContext(trade=_trade(wallet="0xunknown", side="BUY"))
    result = await f.check(ctx)
    assert result.passed is True


async def test_wallet_address_lowercase_normalization(
    session_factory: async_sessionmaker[AsyncSession],
    target_repo: TargetTraderRepository,
) -> None:
    """Lookup case-insensitive : UPPERCASE input matche lowercase storage."""
    await target_repo.insert_shadow("0xmix")
    await target_repo.transition_status("0xmix", new_status="active")
    await target_repo.transition_status("0xmix", new_status="sell_only")
    f = TraderLifecycleFilter(session_factory, _settings())
    ctx = PipelineContext(trade=_trade(wallet="0xMIX", side="BUY"))
    result = await f.check(ctx)
    assert result.passed is False
    assert result.reason == "wallet_sell_only"


# Pytest asyncio mode via pyproject
pytestmark = pytest.mark.asyncio
