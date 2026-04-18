"""Repositories SQLAlchemy 2.0 async pour la couche storage."""

from datetime import UTC, datetime
from typing import Literal

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.storage.dtos import (
    DetectedTradeDTO,
    MyOrderDTO,
    PnlSnapshotDTO,
    RealisticSimulatedOrderDTO,
    StrategyDecisionDTO,
    TraderEventDTO,
    TraderScoreDTO,
)
from polycopy.storage.models import (
    DetectedTrade,
    MyOrder,
    MyPosition,
    PnlSnapshot,
    StrategyDecision,
    TargetTrader,
    TraderEvent,
    TraderScore,
)

log = structlog.get_logger(__name__)


_TraderStatus = Literal["shadow", "active", "paused", "pinned"]
_StatusTransition = Literal["shadow", "active", "paused"]


class TargetTraderRepository:
    """Repository des wallets cibles observés par le watcher."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_active(self) -> list[TargetTrader]:
        """Retourne les traders actuellement suivis par le Watcher.

        Defense in depth M5 : filtre sur ``active=True`` **ET**
        ``status IN ('active', 'pinned')``. Si l'invariante glisse (bug M5),
        on protège le watcher d'aller poller un wallet en `shadow` ou `paused`.
        """
        async with self._session_factory() as session:
            stmt = select(TargetTrader).where(
                TargetTrader.active.is_(True),
                TargetTrader.status.in_(("active", "pinned")),
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def list_all(self) -> list[TargetTrader]:
        """Retourne TOUS les traders (tous statuts), pour cycle de scoring M5."""
        async with self._session_factory() as session:
            stmt = select(TargetTrader)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def list_by_status(self, status: _TraderStatus) -> list[TargetTrader]:
        """Retourne les traders d'un statut donné."""
        async with self._session_factory() as session:
            stmt = select(TargetTrader).where(TargetTrader.status == status)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def count_by_status(self, status: _TraderStatus) -> int:
        """Compte les traders d'un statut donné (utilisé pour le cap MAX_ACTIVE_TRADERS)."""
        async with self._session_factory() as session:
            stmt = select(func.count(TargetTrader.id)).where(TargetTrader.status == status)
            result = await session.execute(stmt)
            return int(result.scalar_one())

    async def get(self, wallet_address: str) -> TargetTrader | None:
        """Fetch un trader par adresse (lowercase)."""
        async with self._session_factory() as session:
            stmt = select(TargetTrader).where(
                TargetTrader.wallet_address == wallet_address.lower(),
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self,
        wallet_address: str,
        label: str | None = None,
    ) -> TargetTrader:
        """Insère ou réactive un trader cible (`pinned`). Adresse normalisée en lowercase.

        Usage M1/M2/M3/M4 : appelé au boot pour les wallets `TARGET_WALLETS` env.
        Les traders re-poussés par ce chemin sont toujours ``pinned`` (whitelist
        user autoritaire) — M5 ne les demote jamais.
        """
        wallet_lower = wallet_address.lower()
        async with self._session_factory() as session:
            stmt = select(TargetTrader).where(TargetTrader.wallet_address == wallet_lower)
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                existing.active = True
                existing.status = "pinned"
                existing.pinned = True
                if label is not None:
                    existing.label = label
                await session.commit()
                await session.refresh(existing)
                return existing
            trader = TargetTrader(
                wallet_address=wallet_lower,
                label=label,
                active=True,
                status="pinned",
                pinned=True,
            )
            session.add(trader)
            await session.commit()
            await session.refresh(trader)
            return trader

    async def insert_shadow(
        self,
        wallet_address: str,
        *,
        label: str | None = None,
        discovered_at: datetime | None = None,
    ) -> TargetTrader:
        """Insère un wallet auto-découvert en ``status='shadow'`` (observation M5).

        ``active=False`` — le watcher ne le pollera pas jusqu'à la promotion.
        ``pinned=False`` — M5 peut promote/demote librement.
        """
        wallet_lower = wallet_address.lower()
        async with self._session_factory() as session:
            stmt = select(TargetTrader).where(TargetTrader.wallet_address == wallet_lower)
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                return existing
            trader = TargetTrader(
                wallet_address=wallet_lower,
                label=label,
                active=False,
                status="shadow",
                pinned=False,
                discovered_at=discovered_at or datetime.now(tz=UTC),
            )
            session.add(trader)
            await session.commit()
            await session.refresh(trader)
            return trader

    async def update_score(
        self,
        wallet_address: str,
        *,
        score: float,
        scoring_version: str,
        scored_at: datetime | None = None,
    ) -> None:
        """Overwrite ``target_traders.score`` + ``last_scored_at`` + ``scoring_version``."""
        wallet_lower = wallet_address.lower()
        async with self._session_factory() as session:
            stmt = select(TargetTrader).where(TargetTrader.wallet_address == wallet_lower)
            trader = (await session.execute(stmt)).scalar_one_or_none()
            if trader is None:
                return
            trader.score = score
            trader.scoring_version = scoring_version
            trader.last_scored_at = scored_at or datetime.now(tz=UTC)
            await session.commit()

    async def transition_status(
        self,
        wallet_address: str,
        *,
        new_status: _StatusTransition,
        reset_hysteresis: bool = False,
    ) -> TargetTrader:
        """Change atomiquement le statut d'un trader.

        - Met ``active=True`` si ``new_status='active'``, sinon ``active=False``.
        - Set ``promoted_at`` à l'instant si transition vers ``'active'``.
        - Reset ``consecutive_low_score_cycles=0`` si ``reset_hysteresis=True``.
        - **Raise ValueError** si le wallet est ``pinned`` — les pinned sont
          intouchables par M5 (spec §2.4 + §2.5, safeguard non-négociable).
        """
        wallet_lower = wallet_address.lower()
        async with self._session_factory() as session:
            stmt = select(TargetTrader).where(TargetTrader.wallet_address == wallet_lower)
            trader = (await session.execute(stmt)).scalar_one_or_none()
            if trader is None:
                raise ValueError(f"TargetTrader not found for wallet {wallet_lower}")
            if trader.pinned:
                raise ValueError(
                    f"Cannot transition_status on pinned wallet {wallet_lower} "
                    f"(from={trader.status!r} to={new_status!r})",
                )
            trader.status = new_status
            trader.active = new_status == "active"
            if new_status == "active":
                trader.promoted_at = datetime.now(tz=UTC)
            if reset_hysteresis:
                trader.consecutive_low_score_cycles = 0
            await session.commit()
            await session.refresh(trader)
            return trader

    async def increment_low_score(self, wallet_address: str) -> int:
        """Incrémente `consecutive_low_score_cycles`, retourne la nouvelle valeur."""
        wallet_lower = wallet_address.lower()
        async with self._session_factory() as session:
            stmt = select(TargetTrader).where(TargetTrader.wallet_address == wallet_lower)
            trader = (await session.execute(stmt)).scalar_one_or_none()
            if trader is None:
                raise ValueError(f"TargetTrader not found for wallet {wallet_lower}")
            trader.consecutive_low_score_cycles += 1
            await session.commit()
            return trader.consecutive_low_score_cycles

    async def reset_low_score(self, wallet_address: str) -> None:
        """Remet `consecutive_low_score_cycles=0`."""
        wallet_lower = wallet_address.lower()
        async with self._session_factory() as session:
            stmt = select(TargetTrader).where(TargetTrader.wallet_address == wallet_lower)
            trader = (await session.execute(stmt)).scalar_one_or_none()
            if trader is None:
                return
            trader.consecutive_low_score_cycles = 0
            await session.commit()


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
            realistic_fill=dto.realistic_fill,
        )
        async with self._session_factory() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def insert_realistic_simulated(
        self,
        dto: RealisticSimulatedOrderDTO,
    ) -> MyOrder:
        """Persiste un ordre M8 (dry-run + realistic_fill) avec ``status``
        ``SIMULATED`` (fill virtuel) ou ``REJECTED`` (FOK strict, book trop fin).

        Garde-fou : ``simulated=True`` et ``realistic_fill=True`` forcés.
        """
        record = MyOrder(
            source_tx_hash=dto.source_tx_hash,
            clob_order_id=None,
            condition_id=dto.condition_id,
            asset_id=dto.asset_id,
            side=dto.side,
            size=dto.size,
            price=dto.price,
            tick_size=dto.tick_size,
            neg_risk=dto.neg_risk,
            order_type=dto.order_type,
            status=dto.status,
            simulated=True,
            realistic_fill=True,
            error_msg=dto.error_msg,
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
        """Met à jour la position **réelle** après un fill CLOB.

        BUY : cumul de size + recalcul de `avg_price` (moyenne pondérée).
        SELL : décrément de size ; si `size <= 0`, marque la position fermée.

        M8 : filtre ``simulated=False`` pour ne jamais toucher une position
        virtuelle depuis le path live.
        """
        async with self._session_factory() as session:
            stmt = select(MyPosition).where(
                MyPosition.condition_id == condition_id,
                MyPosition.asset_id == asset_id,
                MyPosition.closed_at.is_(None),
                MyPosition.simulated.is_(False),
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
                    simulated=False,
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
        """Retourne les positions **réelles** ouvertes (``simulated=False``)."""
        async with self._session_factory() as session:
            stmt = select(MyPosition).where(
                MyPosition.closed_at.is_(None),
                MyPosition.simulated.is_(False),
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_open(self, condition_id: str) -> MyPosition | None:
        """Retourne la position **réelle** ouverte sur le `condition_id`, ou None.

        Filtre ``simulated=False`` pour ségrégation M8 (ne mélange pas réel et
        virtuel — un appelant qui veut le virtuel doit utiliser
        ``list_open_virtual`` ou ``get_open_virtual``).
        """
        async with self._session_factory() as session:
            stmt = select(MyPosition).where(
                MyPosition.condition_id == condition_id,
                MyPosition.closed_at.is_(None),
                MyPosition.simulated.is_(False),
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    # --- M8 : positions virtuelles (dry-run realistic fill) -----------------

    async def upsert_virtual(
        self,
        *,
        condition_id: str,
        asset_id: str,
        side: Literal["BUY", "SELL"],
        size_filled: float,
        fill_price: float,
    ) -> MyPosition | None:
        """Upsert d'une position **virtuelle** sur (condition_id, asset_id).

        BUY : crée ou cumule la position (moyenne pondérée du prix).
        SELL : décrémente une position virtuelle existante (close si ≤ 0). Si
        aucune position virtuelle ouverte n'existe → retourne ``None`` et
        l'appelant log un warning (v1 M8 : skip + warning, pas de crash).
        """
        async with self._session_factory() as session:
            stmt = select(MyPosition).where(
                MyPosition.condition_id == condition_id,
                MyPosition.asset_id == asset_id,
                MyPosition.simulated.is_(True),
                MyPosition.closed_at.is_(None),
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is None:
                if side == "SELL":
                    return None
                position = MyPosition(
                    condition_id=condition_id,
                    asset_id=asset_id,
                    size=size_filled,
                    avg_price=fill_price,
                    simulated=True,
                )
                session.add(position)
                await session.commit()
                await session.refresh(position)
                return position
            if side == "BUY":
                new_size = existing.size + size_filled
                if new_size > 0:
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

    async def list_open_virtual(self) -> list[MyPosition]:
        """Positions virtuelles encore ouvertes (``simulated=True, closed_at=NULL``)."""
        async with self._session_factory() as session:
            stmt = select(MyPosition).where(
                MyPosition.simulated.is_(True),
                MyPosition.closed_at.is_(None),
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def close_virtual(
        self,
        position_id: int,
        *,
        closed_at: datetime,
        realized_pnl: float,
    ) -> None:
        """Close une position virtuelle et persiste le PnL réalisé.

        Garde-fou : refuse si la position n'est pas ``simulated=True`` (defense
        in depth — on ne touche jamais à une vraie position via cette méthode).
        """
        async with self._session_factory() as session:
            position = await session.get(MyPosition, position_id)
            if position is None:
                raise ValueError(f"MyPosition id={position_id} not found")
            if not position.simulated:
                raise ValueError(
                    f"MyPosition id={position_id} is not virtual — "
                    "close_virtual must only be called on simulated positions",
                )
            position.closed_at = closed_at
            position.realized_pnl = realized_pnl
            await session.commit()

    async def sum_realized_pnl_virtual(self) -> float:
        """Somme des ``realized_pnl`` des positions virtuelles fermées."""
        async with self._session_factory() as session:
            stmt = select(func.coalesce(func.sum(MyPosition.realized_pnl), 0.0)).where(
                MyPosition.simulated.is_(True),
                MyPosition.closed_at.is_not(None),
            )
            result = await session.execute(stmt)
            value = result.scalar_one()
            return float(value) if value is not None else 0.0


class PnlSnapshotRepository:
    """Repository des snapshots PnL. Append-only."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def insert(self, dto: PnlSnapshotDTO) -> PnlSnapshot:
        """Persiste un snapshot et retourne l'instance avec son ``id``."""
        record = PnlSnapshot(
            total_usdc=dto.total_usdc,
            realized_pnl=dto.realized_pnl,
            unrealized_pnl=dto.unrealized_pnl,
            drawdown_pct=dto.drawdown_pct,
            open_positions_count=dto.open_positions_count,
            cash_pnl_total=dto.cash_pnl_total,
            is_dry_run=dto.is_dry_run,
        )
        async with self._session_factory() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def get_max_total_usdc(self, *, only_real: bool = True) -> float | None:
        """Retourne le max historique de ``total_usdc`` (pour drawdown all-time-high)."""
        async with self._session_factory() as session:
            stmt = select(func.max(PnlSnapshot.total_usdc))
            if only_real:
                stmt = stmt.where(PnlSnapshot.is_dry_run.is_(False))
            result = await session.execute(stmt)
            value = result.scalar_one_or_none()
            return float(value) if value is not None else None

    async def get_latest(self, *, only_real: bool = True) -> PnlSnapshot | None:
        """Retourne le snapshot le plus récent, ou ``None`` si vide."""
        async with self._session_factory() as session:
            stmt = select(PnlSnapshot).order_by(PnlSnapshot.timestamp.desc()).limit(1)
            if only_real:
                stmt = stmt.where(PnlSnapshot.is_dry_run.is_(False))
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def list_since(
        self,
        since: datetime,
        *,
        only_real: bool = True,
    ) -> list[PnlSnapshot]:
        """Retourne les snapshots depuis ``since`` (timestamp ascendant)."""
        async with self._session_factory() as session:
            stmt = select(PnlSnapshot).where(PnlSnapshot.timestamp >= since)
            if only_real:
                stmt = stmt.where(PnlSnapshot.is_dry_run.is_(False))
            stmt = stmt.order_by(PnlSnapshot.timestamp.asc())
            result = await session.execute(stmt)
            return list(result.scalars().all())


# --- M5 discovery repositories ------------------------------------------------


class TraderScoreRepository:
    """Repository append-only des scores historiques (1 ligne par wallet × cycle)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def insert(self, dto: TraderScoreDTO) -> TraderScore:
        """Persiste un score M5 avec son snapshot de metrics."""
        record = TraderScore(
            target_trader_id=dto.target_trader_id,
            wallet_address=dto.wallet_address.lower(),
            score=dto.score,
            scoring_version=dto.scoring_version,
            low_confidence=dto.low_confidence,
            metrics_snapshot=dto.metrics_snapshot,
        )
        async with self._session_factory() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def latest_for_wallet(self, wallet_address: str) -> TraderScore | None:
        """Dernier score connu pour un wallet (ou None)."""
        async with self._session_factory() as session:
            stmt = (
                select(TraderScore)
                .where(TraderScore.wallet_address == wallet_address.lower())
                .order_by(TraderScore.cycle_at.desc())
                .limit(1)
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def list_for_wallet(
        self,
        wallet_address: str,
        *,
        limit: int = 100,
    ) -> list[TraderScore]:
        """Historique de scores pour un wallet, ordre chronologique décroissant."""
        async with self._session_factory() as session:
            stmt = (
                select(TraderScore)
                .where(TraderScore.wallet_address == wallet_address.lower())
                .order_by(TraderScore.cycle_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def latest_per_wallet(self, *, limit: int = 200) -> list[TraderScore]:
        """Pour le dashboard /traders : 1 ligne par wallet, le score le plus récent.

        Implémenté en sous-requête `max(cycle_at) group by wallet_address`.
        """
        async with self._session_factory() as session:
            latest_at = (
                select(
                    TraderScore.wallet_address,
                    func.max(TraderScore.cycle_at).label("max_cycle_at"),
                )
                .group_by(TraderScore.wallet_address)
                .subquery()
            )
            stmt = (
                select(TraderScore)
                .join(
                    latest_at,
                    (TraderScore.wallet_address == latest_at.c.wallet_address)
                    & (TraderScore.cycle_at == latest_at.c.max_cycle_at),
                )
                .order_by(TraderScore.score.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())


class TraderEventRepository:
    """Repository append-only des événements du lifecycle discovery (audit trail)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def insert(self, dto: TraderEventDTO) -> TraderEvent:
        """Persiste un événement (non effacé même après demote/remove)."""
        record = TraderEvent(
            wallet_address=dto.wallet_address.lower(),
            event_type=dto.event_type,
            from_status=dto.from_status,
            to_status=dto.to_status,
            score_at_event=dto.score_at_event,
            scoring_version=dto.scoring_version,
            reason=dto.reason,
            event_metadata=dto.event_metadata,
        )
        async with self._session_factory() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    async def list_recent(
        self,
        *,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[TraderEvent]:
        """Derniers événements, ordre chronologique décroissant."""
        async with self._session_factory() as session:
            stmt = select(TraderEvent)
            if since is not None:
                stmt = stmt.where(TraderEvent.at >= since)
            stmt = stmt.order_by(TraderEvent.at.desc()).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def count_by_event_type_since(self, since: datetime) -> dict[str, int]:
        """Agrégation par event_type sur la fenêtre — utilisé pour KPIs dashboard Home."""
        async with self._session_factory() as session:
            stmt = (
                select(TraderEvent.event_type, func.count(TraderEvent.id))
                .where(TraderEvent.at >= since)
                .group_by(TraderEvent.event_type)
            )
            result = await session.execute(stmt)
            return {row[0]: int(row[1]) for row in result.all()}
