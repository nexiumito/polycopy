"""Test smoke du WatcherOrchestrator (TaskGroup, sortie propre, no-targets)."""

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
    await asyncio.wait_for(orchestrator.run_forever(), timeout=2.0)


async def test_orchestrator_starts_pollers_and_stops_on_request(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_repo = TargetTraderRepository(session_factory)
    await target_repo.upsert("0xWALLET")

    call_count = 0

    async def _fake_get_trades(self: object, wallet: str, since: object = None, limit: int = 100) -> list[object]:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        return []

    monkeypatch.setattr(
        data_api_client_module.DataApiClient,
        "get_trades",
        _fake_get_trades,
    )

    orchestrator = WatcherOrchestrator(session_factory, _settings())

    async def _stop_soon() -> None:
        await asyncio.sleep(0.2)
        orchestrator.request_stop()

    await asyncio.gather(orchestrator.run_forever(), _stop_soon())
    assert call_count >= 1
