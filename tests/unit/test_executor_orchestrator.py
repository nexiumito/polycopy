"""Tests du `ExecutorOrchestrator` (init garde-fous + queue → pipeline + shutdown)."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor import orchestrator as orchestrator_module
from polycopy.executor.dtos import ExecutorAuthError
from polycopy.executor.orchestrator import ExecutorOrchestrator
from polycopy.storage.repositories import MyOrderRepository
from polycopy.strategy.dtos import OrderApproved


def _dry_settings() -> Settings:
    return Settings(_env_file=None, execution_mode="dry_run")  # type: ignore[call-arg]


def _real_settings_no_keys() -> Settings:
    return Settings(_env_file=None, execution_mode="live")  # type: ignore[call-arg]


def _approved() -> OrderApproved:
    return OrderApproved(
        detected_trade_id=0,
        tx_hash="0xtx_orch_exec",
        condition_id="0xcond",
        asset_id="123",
        side="BUY",
        my_size=10.0,
        my_price=0.08,
    )


@pytest.fixture(autouse=True)
def _fast_queue_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator_module, "_QUEUE_GET_TIMEOUT_SECONDS", 0.05)


async def _stop_after(stop_event: asyncio.Event, delay: float) -> None:
    await asyncio.sleep(delay)
    stop_event.set()


# --- Garde-fou démarrage §2.2 ----------------------------------------------


def test_constructor_live_mode_without_keys_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """M10 §3.5.2 : garde-fou 2 — RuntimeError si EXECUTION_MODE=live + keys None."""
    queue: asyncio.Queue[OrderApproved] = asyncio.Queue()
    with pytest.raises(RuntimeError, match="EXECUTION_MODE=live"):
        ExecutorOrchestrator(session_factory, _real_settings_no_keys(), queue)


def test_constructor_dry_run_no_keys_ok(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    queue: asyncio.Queue[OrderApproved] = asyncio.Queue()
    orch = ExecutorOrchestrator(session_factory, _dry_settings(), queue)
    assert orch is not None


# --- Run dry-run path -------------------------------------------------------


async def test_run_dry_run_simulates_one_order(
    session_factory: async_sessionmaker[AsyncSession],
    my_order_repo: MyOrderRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stub Gamma + tick_size pour pipeline interne sans réseau.
    async def _gamma_stub(self: object, condition_id: str) -> object:
        del self, condition_id
        from polycopy.strategy.dtos import MarketMetadata

        return MarketMetadata(
            id="1", conditionId="0xcond", clobTokenIds='["123"]', outcomes='["Yes"]'
        )

    async def _tick_stub(self: object, token_id: str) -> float:
        del self, token_id
        return 0.01

    monkeypatch.setattr("polycopy.strategy.gamma_client.GammaApiClient.get_market", _gamma_stub)
    monkeypatch.setattr(
        "polycopy.executor.clob_metadata_client.ClobMetadataClient.get_tick_size",
        _tick_stub,
    )

    queue: asyncio.Queue[OrderApproved] = asyncio.Queue()
    queue.put_nowait(_approved())
    orch = ExecutorOrchestrator(session_factory, _dry_settings(), queue)
    stop_event = asyncio.Event()
    await asyncio.gather(
        orch.run_forever(stop_event),
        _stop_after(stop_event, 0.3),
    )
    recent = await my_order_repo.list_recent(limit=1)
    assert len(recent) == 1
    assert recent[0].status == "SIMULATED"
    assert recent[0].simulated is True


# --- ExecutorAuthError fatal -----------------------------------------------


async def test_run_propagates_auth_error_and_sets_stop(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si execute_order raise ExecutorAuthError, l'orchestrateur stop_event.set() + raise."""
    fake_execute = AsyncMock(side_effect=ExecutorAuthError("invalid creds"))
    monkeypatch.setattr(orchestrator_module, "execute_order", fake_execute)

    queue: asyncio.Queue[OrderApproved] = asyncio.Queue()
    queue.put_nowait(_approved())
    orch = ExecutorOrchestrator(session_factory, _dry_settings(), queue)
    stop_event = asyncio.Event()
    with pytest.raises(ExecutorAuthError):
        await orch.run_forever(stop_event)
    assert stop_event.is_set()


# --- Exception générique → loop continue, ne crash pas ---------------------


async def test_run_continues_after_generic_exception(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_execute = AsyncMock(side_effect=RuntimeError("transient blip"))
    monkeypatch.setattr(orchestrator_module, "execute_order", fake_execute)

    queue: asyncio.Queue[OrderApproved] = asyncio.Queue()
    queue.put_nowait(_approved())
    orch = ExecutorOrchestrator(session_factory, _dry_settings(), queue)
    stop_event = asyncio.Event()
    # Ne doit pas raise.
    await asyncio.gather(
        orch.run_forever(stop_event),
        _stop_after(stop_event, 0.2),
    )
    fake_execute.assert_called()
