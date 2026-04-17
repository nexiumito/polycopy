"""Repositories SQLAlchemy 2.0 async pour la couche storage."""

from datetime import UTC, datetime
from typing import Literal

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.storage.dtos import DetectedTradeDTO, MyOrderDTO, StrategyDecisionDTO
from polycopy.storage.models import (
    DetectedTrade,
    MyOrder,
    MyPosition,
    StrategyDecision,
    TargetTrader,
)

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


class StrategyDecisionRepository:
    """Repository des décisions du pipeline strategy. Append-only (jamais d'update)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def insert(self, decision: StrategyDecisionDTO) -> StrategyDecision:
        """Persiste la décision et retourne l'instance avec son `id`."""
        record = StrategyDecision(
            detected_trade_id=decision.detected_trade_id,
            tx_hash=decision.tx_hash,
            decision=decision.decision,
            reason=decision.reason,
            my_size=decision.my_size,
            my_price=decision.my_price,
            slippage_pct=decision.slippage_pct,
            pipeline_state=decision.pipeline_state,
        )
        async with self._session_factory() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def list_recent(self, limit: int = 100) -> list[StrategyDecision]:
        """Retourne les décisions les plus récentes (debug)."""
        async with self._session_factory() as session:
            stmt = (
                select(StrategyDecision).order_by(StrategyDecision.decided_at.desc()).limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def count_by_decision(self) -> dict[str, int]:
        """Compte les décisions par type (`APPROVED` / `REJECTED`) — métrics."""
        async with self._session_factory() as session:
            stmt = select(
                StrategyDecision.decision,
                func.count(StrategyDecision.id),
            ).group_by(StrategyDecision.decision)
            result = await session.execute(stmt)
            return {row[0]: int(row[1]) for row in result.all()}


_OrderStatus = Literal["SIMULATED", "SENT", "FILLED", "PARTIALLY_FILLED", "REJECTED", "FAILED"]


class MyOrderRepository:
    """Repository des ordres envoyés (ou simulés) par l'Executor. Append-only."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def insert(self, dto: MyOrderDTO) -> MyOrder:
        """Persiste un nouvel ordre et retourne l'instance avec son `id`."""
        record = MyOrder(
            source_tx_hash=dto.source_tx_hash,
            clob_order_id=dto.clob_order_id,
            condition_id=dto.condition_id,
            asset_id=dto.asset_id,
            side=dto.side,
            size=dto.size,
            price=dto.price,
            tick_size=dto.tick_size,
            neg_risk=dto.neg_risk,
            order_type=dto.order_type,
            status=dto.status,
            simulated=dto.simulated,
        )
        async with self._session_factory() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def update_status(
        self,
        order_id: int,
        status: _OrderStatus,
        *,
        clob_order_id: str | None = None,
        taking_amount: str | None = None,
        making_amount: str | None = None,
        transaction_hashes: list[str] | None = None,
        error_msg: str | None = None,
        filled_at: datetime | None = None,
    ) -> None:
        """Met à jour le statut et les champs d'exécution d'un ordre existant."""
        async with self._session_factory() as session:
            order = await session.get(MyOrder, order_id)
            if order is None:
                raise ValueError(f"MyOrder id={order_id} not found")
            order.status = status
            if clob_order_id is not None:
                order.clob_order_id = clob_order_id
            if taking_amount is not None:
                order.taking_amount = taking_amount
            if making_amount is not None:
                order.making_amount = making_amount
            if transaction_hashes is not None:
                order.transaction_hashes = transaction_hashes
            if error_msg is not None:
                order.error_msg = error_msg
            if filled_at is not None:
                order.filled_at = filled_at
            await session.commit()

    async def list_recent(self, limit: int = 100) -> list[MyOrder]:
        """Retourne les `limit` ordres les plus récents (par `sent_at` desc)."""
        async with self._session_factory() as session:
            stmt = select(MyOrder).order_by(MyOrder.sent_at.desc()).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())


class MyPositionRepository:
    """Repository des positions ouvertes. Mises à jour incrémentales sur fill."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert_on_fill(
        self,
        condition_id: str,
        asset_id: str,
        side: Literal["BUY", "SELL"],
        size_filled: float,
        fill_price: float,
    ) -> MyPosition:
        """Met à jour la position après un fill.

        BUY : cumul de size + recalcul de `avg_price` (moyenne pondérée).
        SELL : décrément de size ; si `size <= 0`, marque la position fermée.
        """
        async with self._session_factory() as session:
            stmt = select(MyPosition).where(
                MyPosition.condition_id == condition_id,
                MyPosition.asset_id == asset_id,
                MyPosition.closed_at.is_(None),
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is None:
                if side == "SELL":
                    raise ValueError(
                        f"Cannot SELL {size_filled} on a non-existent position "
                        f"(condition_id={condition_id}, asset_id={asset_id})"
                    )
                position = MyPosition(
                    condition_id=condition_id,
                    asset_id=asset_id,
                    size=size_filled,
                    avg_price=fill_price,
                )
                session.add(position)
                await session.commit()
                await session.refresh(position)
                return position
            if side == "BUY":
                new_size = existing.size + size_filled
                existing.avg_price = (
                    existing.size * existing.avg_price + size_filled * fill_price
                ) / new_size
                existing.size = new_size
            else:  # SELL
                existing.size -= size_filled
                if existing.size <= 0:
                    existing.closed_at = datetime.now(tz=UTC)
            await session.commit()
            await session.refresh(existing)
            return existing

    async def list_open(self) -> list[MyPosition]:
        """Retourne toutes les positions ouvertes (`closed_at IS NULL`)."""
        async with self._session_factory() as session:
            stmt = select(MyPosition).where(MyPosition.closed_at.is_(None))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_open(self, condition_id: str) -> MyPosition | None:
        """Retourne la position ouverte sur le `condition_id`, ou None."""
        async with self._session_factory() as session:
            stmt = select(MyPosition).where(
                MyPosition.condition_id == condition_id,
                MyPosition.closed_at.is_(None),
            )
            return (await session.execute(stmt)).scalar_one_or_none()
