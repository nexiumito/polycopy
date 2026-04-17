"""Tests du `StrategyOrchestrator` : queue → pipeline → persist + shutdown."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.repositories import StrategyDecisionRepository
from polycopy.strategy import orchestrator as orchestrator_module
from polycopy.strategy.dtos import MarketMetadata, OrderApproved
from polycopy.strategy.orchestrator import StrategyOrchestrator


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        copy_ratio=0.01,
        max_position_usd=100.0,
        min_market_liquidity_usd=1000.0,
        min_hours_to_expiry=1.0,
        max_slippage_pct=5.0,
        risk_available_capital_usd_stub=1000.0,
    )


def _trade() -> DetectedTradeDTO:
    return DetectedTradeDTO(
        tx_hash="0xtx_orch",
        target_wallet="0xw",
        condition_id="0xc",
        asset_id="123",
        side="BUY",
        size=100.0,
        usdc_size=8.0,
        price=0.08,
        timestamp=datetime.now(tz=UTC),
        raw_json={},
    )


def _market_ok() -> MarketMetadata:
    return MarketMetadata(
        id="1",
        conditionId="0xc",
        active=True,
        closed=False,
        archived=False,
        acceptingOrders=True,
        enableOrderBook=True,
        liquidityClob=50000.0,
        endDate=datetime.now(tz=UTC) + timedelta(days=30),
        clobTokenIds='["123","456"]',
        outcomes='["Yes","No"]',
    )


@pytest.fixture(autouse=True)
def _fast_queue_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator_module, "_QUEUE_GET_TIMEOUT_SECONDS", 0.05)


async def _stop_after(stop_event: asyncio.Event, delay: float) -> None:
    await asyncio.sleep(delay)
    stop_event.set()


async def test_orchestrator_pulls_pipeline_persists_pushes_approved(
    session_factory: async_sessionmaker[AsyncSession],
    strategy_decision_repo: StrategyDecisionRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Push 1 trade qui passe → persist APPROVED + push OrderApproved."""

    async def _gamma(self: object, condition_id: str) -> MarketMetadata | None:
        del self, condition_id
        return _market_ok()

    async def _clob(self: object, token_id: str) -> float | None:
        del self, token_id
        return 0.0805

    monkeypatch.setattr("polycopy.strategy.gamma_client.GammaApiClient.get_market", _gamma)
    monkeypatch.setattr("polycopy.strategy.clob_read_client.ClobReadClient.get_midpoint", _clob)

    in_q: asyncio.Queue[DetectedTradeDTO] = asyncio.Queue()
    out_q: asyncio.Queue[OrderApproved] = asyncio.Queue()
    in_q.put_nowait(_trade())

    stop_event = asyncio.Event()
    orchestrator = StrategyOrchestrator(session_factory, _settings(), in_q, out_q)
    await asyncio.gather(
        orchestrator.run_forever(stop_event),
        _stop_after(stop_event, 0.3),
    )

    counts = await strategy_decision_repo.count_by_decision()
    assert counts.get("APPROVED", 0) == 1
    assert out_q.qsize() == 1
    pushed = out_q.get_nowait()
    assert pushed.tx_hash == "0xtx_orch"


async def test_orchestrator_rejected_does_not_push(
    session_factory: async_sessionmaker[AsyncSession],
    strategy_decision_repo: StrategyDecisionRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trade rejeté (market_not_found) → REJECTED persisté, queue out vide."""

    async def _gamma(self: object, condition_id: str) -> MarketMetadata | None:
        del self, condition_id
        return None

    monkeypatch.setattr("polycopy.strategy.gamma_client.GammaApiClient.get_market", _gamma)

    in_q: asyncio.Queue[DetectedTradeDTO] = asyncio.Queue()
    out_q: asyncio.Queue[OrderApproved] = asyncio.Queue()
    in_q.put_nowait(_trade())

    stop_event = asyncio.Event()
    orchestrator = StrategyOrchestrator(session_factory, _settings(), in_q, out_q)
    await asyncio.gather(
        orchestrator.run_forever(stop_event),
        _stop_after(stop_event, 0.3),
    )

    counts = await strategy_decision_repo.count_by_decision()
    assert counts.get("REJECTED", 0) == 1
    assert out_q.empty()


async def test_orchestrator_pipeline_exception_persists_error(
    session_factory: async_sessionmaker[AsyncSession],
    strategy_decision_repo: StrategyDecisionRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception non gérée du pipeline → REJECTED reason=pipeline_error, ne crash pas."""

    async def _boom(self: object, condition_id: str) -> Any:
        del self, condition_id
        raise RuntimeError("gamma down")

    monkeypatch.setattr("polycopy.strategy.gamma_client.GammaApiClient.get_market", _boom)

    in_q: asyncio.Queue[DetectedTradeDTO] = asyncio.Queue()
    out_q: asyncio.Queue[OrderApproved] = asyncio.Queue()
    in_q.put_nowait(_trade())

    stop_event = asyncio.Event()
    orchestrator = StrategyOrchestrator(session_factory, _settings(), in_q, out_q)
    await asyncio.gather(
        orchestrator.run_forever(stop_event),
        _stop_after(stop_event, 0.3),
    )

    recent = await strategy_decision_repo.list_recent(limit=10)
    assert any(d.reason == "pipeline_error" for d in recent)


async def test_orchestrator_executor_queue_full_logs_warning(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """approved_orders_queue saturée → warning logué, ne crash pas."""

    async def _gamma(self: object, condition_id: str) -> MarketMetadata | None:
        del self, condition_id
        return _market_ok()

    async def _clob(self: object, token_id: str) -> float | None:
        del self, token_id
        return 0.0805

    monkeypatch.setattr("polycopy.strategy.gamma_client.GammaApiClient.get_market", _gamma)
    monkeypatch.setattr("polycopy.strategy.clob_read_client.ClobReadClient.get_midpoint", _clob)

    in_q: asyncio.Queue[DetectedTradeDTO] = asyncio.Queue()
    out_q: asyncio.Queue[OrderApproved] = asyncio.Queue(maxsize=1)
    out_q.put_nowait(  # pré-remplir pour saturer
        OrderApproved(
            detected_trade_id=0,
            tx_hash="0xpre",
            condition_id="0xc",
            asset_id="123",
            side="BUY",
            my_size=1.0,
            my_price=0.5,
        ),
    )
    in_q.put_nowait(_trade())

    stop_event = asyncio.Event()
    orchestrator = StrategyOrchestrator(session_factory, _settings(), in_q, out_q)
    # ne doit pas raise
    await asyncio.gather(
        orchestrator.run_forever(stop_event),
        _stop_after(stop_event, 0.3),
    )
