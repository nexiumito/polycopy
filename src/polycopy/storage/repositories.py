"""Repositories SQLAlchemy 2.0 async pour la couche storage."""

from datetime import datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.models import DetectedTrade, TargetTrader

log = structlog.get_logger(__name__)


class TargetTraderRepository:
    """Repository des wallets cibles observés par le watcher."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_active(self) -> list[TargetTrader]:
        """Retourne tous les traders actifs."""
        async with self._session_factory() as session:
            stmt = select(TargetTrader).where(TargetTrader.active.is_(True))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def upsert(
        self,
        wallet_address: str,
        label: str | None = None,
    ) -> TargetTrader:
        """Insère ou réactive un trader cible. Adresse normalisée en lowercase."""
        wallet_lower = wallet_address.lower()
        async with self._session_factory() as session:
            stmt = select(TargetTrader).where(TargetTrader.wallet_address == wallet_lower)
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                existing.active = True
                if label is not None:
                    existing.label = label
                await session.commit()
                await session.refresh(existing)
                return existing
            trader = TargetTrader(wallet_address=wallet_lower, label=label, active=True)
            session.add(trader)
            await session.commit()
            await session.refresh(trader)
            return trader


class DetectedTradeRepository:
    """Repository des trades détectés on-chain. Dédup par `tx_hash`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def insert_if_new(self, trade: DetectedTradeDTO) -> bool:
        """Insère le trade ; retourne True si nouveau, False si `tx_hash` déjà connu."""
        record = DetectedTrade(
            tx_hash=trade.tx_hash,
            target_wallet=trade.target_wallet.lower(),
            condition_id=trade.condition_id,
            asset_id=trade.asset_id,
            side=trade.side,
            size=trade.size,
            usdc_size=trade.usdc_size,
            price=trade.price,
            timestamp=trade.timestamp,
            outcome=trade.outcome,
            slug=trade.slug,
            raw_json=trade.raw_json,
        )
        async with self._session_factory() as session:
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            return True

    async def get_latest_timestamp(self, wallet: str) -> datetime | None:
        """Retourne le `max(timestamp)` connu pour le wallet, ou None si vide."""
        async with self._session_factory() as session:
            stmt = select(func.max(DetectedTrade.timestamp)).where(
                DetectedTrade.target_wallet == wallet.lower(),
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def count_for_wallet(self, wallet: str) -> int:
        """Nombre total de trades persistés pour le wallet (utilitaire debug)."""
        async with self._session_factory() as session:
            stmt = select(func.count(DetectedTrade.id)).where(
                DetectedTrade.target_wallet == wallet.lower(),
            )
            result = await session.execute(stmt)
            return int(result.scalar_one())
