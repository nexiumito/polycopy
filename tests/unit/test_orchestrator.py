"""Test smoke du WatcherOrchestrator (TaskGroup, sortie propre, no-targets).

Depuis M2, l'orchestrator ne possède plus son propre `stop_event` ; il en reçoit
un de `__main__`. Les tests construisent leur propre event.
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.storage.repositories import TargetTraderRepository
from polycopy.watcher import data_api_client as data_api_client_module
from polycopy.watcher.orchestrator import WatcherOrchestrator


def _settings() -> Settings:
    return Settings(_env_file=None, poll_interval_seconds=1)  # type: ignore[call-arg]


async def test_orchestrator_no_targets_returns_quickly(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    orchestrator = WatcherOrchestrator(session_factory, _settings())
    stop_event = asyncio.Event()
    await asyncio.wait_for(orchestrator.run_forever(stop_event), timeout=2.0)


async def test_orchestrator_starts_pollers_and_stops_on_event(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_repo = TargetTraderRepository(session_factory)
    await target_repo.upsert("0xWALLET")

    call_count = 0

    async def _fake_get_trades(
        self: object,
        wallet: str,
        since: object = None,
        limit: int = 100,
    ) -> list[object]:
        del self, wallet, since, limit
        nonlocal call_count
        call_count += 1
        return []

    monkeypatch.setattr(
        data_api_client_module.DataApiClient,
        "get_trades",
        _fake_get_trades,
    )

    orchestrator = WatcherOrchestrator(session_factory, _settings())
    stop_event = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.2)
        stop_event.set()

    await asyncio.gather(orchestrator.run_forever(stop_event), _stop_soon())
    assert call_count >= 1


async def test_orchestrator_pushes_to_queue_when_provided(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Régression M2 : la queue optionnelle est bien transmise aux pollers."""
    from datetime import UTC, datetime

    from polycopy.storage.dtos import DetectedTradeDTO
    from polycopy.watcher.dtos import TradeActivity

    target_repo = TargetTraderRepository(session_factory)
    await target_repo.upsert("0xWALLET")

    activity = TradeActivity.model_validate(
        {
            "type": "TRADE",
            "proxyWallet": "0xwallet",
            "timestamp": int(datetime.now(tz=UTC).timestamp()),
            "conditionId": "0xcond",
            "asset": "123",
            "side": "BUY",
            "size": 1.0,
            "usdcSize": 0.5,
            "price": 0.5,
            "transactionHash": "0xtx_orch",
            "outcome": "Yes",
            "slug": "slug",
            "outcomeIndex": 0,
        },
    )

    async def _fake_get_trades(
        self: object,
        wallet: str,
        since: object = None,
        limit: int = 100,
    ) -> list[TradeActivity]:
        del self, wallet, since, limit
        return [activity]

    monkeypatch.setattr(
        data_api_client_module.DataApiClient,
        "get_trades",
        _fake_get_trades,
    )

    queue: asyncio.Queue[DetectedTradeDTO] = asyncio.Queue()
    orchestrator = WatcherOrchestrator(
        session_factory,
        _settings(),
        detected_trades_queue=queue,
    )
    stop_event = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.2)
        stop_event.set()

    await asyncio.gather(orchestrator.run_forever(stop_event), _stop_soon())
    assert queue.qsize() >= 1
    pushed = queue.get_nowait()
    assert pushed.tx_hash == "0xtx_orch"
