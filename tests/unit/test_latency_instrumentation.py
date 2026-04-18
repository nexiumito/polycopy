"""Tests M11 §9.3.D — instrumentation latence end-to-end + repository purge."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.models import TradeLatencySample
from polycopy.storage.repositories import (
    DetectedTradeRepository,
    TradeLatencyRepository,
)


def _dto(trade_id: str | None = None) -> DetectedTradeDTO:
    return DetectedTradeDTO(
        tx_hash="0xtx_abc",
        target_wallet="0xwallet",
        condition_id="0xc",
        asset_id="tok",
        side="BUY",
        size=10.0,
        usdc_size=1.0,
        price=0.5,
        timestamp=datetime.now(tz=UTC),
        raw_json={"a": 1},
        trade_id=trade_id,
    )


async def test_detected_trade_dto_accepts_trade_id() -> None:
    """Nouveau champ optionnel ``trade_id`` accepté."""
    dto = _dto(trade_id="abc123")
    assert dto.trade_id == "abc123"


async def test_detected_trade_dto_default_trade_id_is_none() -> None:
    dto = _dto()
    assert dto.trade_id is None


async def test_latency_repo_insert_and_list_since(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = TradeLatencyRepository(session_factory)
    await repo.insert("tid1", "watcher_detected_ms", 12.5)
    await repo.insert("tid1", "strategy_enriched_ms", 3.2)
    samples = await repo.list_since(datetime.now(tz=UTC) - timedelta(minutes=1))
    assert len(samples) == 2
    names = {s.stage_name for s in samples}
    assert names == {"watcher_detected_ms", "strategy_enriched_ms"}


async def test_latency_repo_purge_older_than(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Les rows > N jours sont supprimés."""
    repo = TradeLatencyRepository(session_factory)
    old_ts = datetime.now(tz=UTC) - timedelta(days=8)
    async with session_factory() as session:
        session.add(
            TradeLatencySample(
                trade_id="old",
                stage_name="watcher_detected_ms",
                duration_ms=42.0,
                timestamp=old_ts,
            ),
        )
        session.add(
            TradeLatencySample(
                trade_id="new",
                stage_name="watcher_detected_ms",
                duration_ms=1.0,
                timestamp=datetime.now(tz=UTC),
            ),
        )
        await session.commit()
    deleted = await repo.purge_older_than(days=7)
    assert deleted == 1
    remaining = await repo.list_since(datetime.now(tz=UTC) - timedelta(days=30))
    assert len(remaining) == 1
    assert remaining[0].trade_id == "new"


async def test_run_pipeline_inserts_latency_rows(
    session_factory: async_sessionmaker[AsyncSession],
    detected_trade_repo: DetectedTradeRepository,
) -> None:
    """Le pipeline instrumenté insère 3 stages (enriched/sized/risk_checked)."""
    from unittest.mock import AsyncMock, MagicMock

    from polycopy.config import Settings
    from polycopy.strategy.pipeline import run_pipeline

    settings = Settings(target_wallets=[])  # défauts M11 : tous les flags true
    assert settings.latency_instrumentation_enabled

    trade = _dto(trade_id="ttest123")

    gamma_client = MagicMock()
    gamma_client.get_market = AsyncMock(return_value=None)  # rejette au 1er filtre
    clob_client = MagicMock()
    clob_client.get_midpoint = AsyncMock(return_value=0.1)

    latency_repo = TradeLatencyRepository(session_factory)
    decision, reason, _ = await run_pipeline(
        trade,
        gamma_client=gamma_client,
        clob_client=clob_client,
        session_factory=session_factory,
        settings=settings,
        ws_client=None,
        latency_repo=latency_repo,
    )
    assert decision == "REJECTED"
    assert reason == "market_not_found"
    samples = await latency_repo.list_since(datetime.now(tz=UTC) - timedelta(minutes=1))
    # Le pipeline s'est arrêté au 1er filtre → seul MarketFilter timing enregistré.
    stage_names = {s.stage_name for s in samples}
    assert "strategy_enriched_ms" in stage_names
    assert all(s.trade_id == "ttest123" for s in samples)


async def test_run_pipeline_no_instrumentation_if_trade_id_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Si ``trade_id is None`` → aucune insertion (backward-compat M2..M10)."""
    from unittest.mock import AsyncMock, MagicMock

    from polycopy.config import Settings
    from polycopy.strategy.pipeline import run_pipeline

    settings = Settings(target_wallets=[])
    trade = _dto(trade_id=None)
    gamma_client = MagicMock()
    gamma_client.get_market = AsyncMock(return_value=None)
    clob_client = MagicMock()
    latency_repo = TradeLatencyRepository(session_factory)
    await run_pipeline(
        trade,
        gamma_client=gamma_client,
        clob_client=clob_client,
        session_factory=session_factory,
        settings=settings,
        latency_repo=latency_repo,
    )
    samples = await latency_repo.list_since(datetime.now(tz=UTC) - timedelta(minutes=1))
    assert samples == []


async def test_run_pipeline_no_instrumentation_if_flag_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Flag ``latency_instrumentation_enabled=False`` → no-op."""
    from unittest.mock import AsyncMock, MagicMock

    from polycopy.config import Settings
    from polycopy.strategy.pipeline import run_pipeline

    settings = Settings(target_wallets=[], latency_instrumentation_enabled=False)
    trade = _dto(trade_id="abc")
    gamma_client = MagicMock()
    gamma_client.get_market = AsyncMock(return_value=None)
    clob_client = MagicMock()
    latency_repo = TradeLatencyRepository(session_factory)
    await run_pipeline(
        trade,
        gamma_client=gamma_client,
        clob_client=clob_client,
        session_factory=session_factory,
        settings=settings,
        latency_repo=latency_repo,
    )
    samples = await latency_repo.list_since(datetime.now(tz=UTC) - timedelta(minutes=1))
    assert samples == []


async def test_latency_purge_scheduler_exits_on_stop_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Le scheduler sort proprement quand ``stop_event`` est set."""
    from polycopy.config import Settings
    from polycopy.storage.latency_purge_scheduler import LatencyPurgeScheduler

    settings = Settings(target_wallets=[], latency_sample_retention_days=7)
    scheduler = LatencyPurgeScheduler(
        TradeLatencyRepository(session_factory),
        settings,
    )
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(scheduler.run_forever(stop), timeout=2.0)
