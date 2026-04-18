"""Tests : les producteurs M2/M3 poussent bien les alertes sur la queue."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor import orchestrator as executor_orchestrator_module
from polycopy.executor.dtos import (
    ExecutorAuthError,
    OrderResult,
    WalletState,
)
from polycopy.executor.orchestrator import ExecutorOrchestrator
from polycopy.executor.pipeline import execute_order
from polycopy.monitoring.dtos import Alert
from polycopy.storage.repositories import MyOrderRepository, MyPositionRepository
from polycopy.strategy.dtos import MarketMetadata, OrderApproved


def _approved() -> OrderApproved:
    return OrderApproved(
        detected_trade_id=0,
        tx_hash="0xtx_producer",
        condition_id="0xcond",
        asset_id="123",
        side="BUY",
        my_size=10.0,
        my_price=0.5,
    )


def _real_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=False,
        polymarket_private_key="0x" + "1" * 64,
        polymarket_funder="0xF",
        alert_large_order_usd_threshold=50.0,
    )


def _stub_clients(
    *,
    order_result: OrderResult | None = None,
    sdk_exception: Exception | None = None,
) -> dict[str, AsyncMock]:
    from polycopy.executor.clob_metadata_client import ClobMetadataClient
    from polycopy.executor.clob_write_client import ClobWriteClient
    from polycopy.executor.wallet_state_reader import WalletStateReader
    from polycopy.strategy.gamma_client import GammaApiClient

    metadata = AsyncMock(spec=ClobMetadataClient)
    metadata.get_tick_size.return_value = 0.01
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_market.return_value = MarketMetadata(
        id="1",
        conditionId="0xcond",
        clobTokenIds='["123"]',
        outcomes='["Yes","No"]',
    )
    wallet = AsyncMock(spec=WalletStateReader)
    wallet.get_state.return_value = WalletState(
        total_position_value_usd=0.0,
        available_capital_usd=1_000_000.0,
        open_positions_count=0,
    )
    write = AsyncMock(spec=ClobWriteClient)
    if sdk_exception is not None:
        write.post_order.side_effect = sdk_exception
    elif order_result is not None:
        write.post_order.return_value = order_result
    return {
        "metadata_client": metadata,
        "gamma_client": gamma,
        "wallet_state_reader": wallet,
        "write_client": write,
    }


# --- SDK exception → Alert(executor_error) ---------------------------------


@pytest.mark.asyncio
async def test_sdk_exception_pushes_executor_error_alert(
    session_factory: async_sessionmaker[AsyncSession],
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    del session_factory
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    clients = _stub_clients(sdk_exception=RuntimeError("boom"))
    await execute_order(
        _approved(),
        settings=_real_settings(),
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        alerts_queue=queue,
        **clients,
    )
    events = {queue.get_nowait().event for _ in range(queue.qsize())}
    assert "executor_error" in events


# --- success matched above threshold → Alert(order_filled_large) -----------


@pytest.mark.asyncio
async def test_large_fill_pushes_order_filled_large_alert(
    session_factory: async_sessionmaker[AsyncSession],
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    del session_factory
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    # takingAmount = 100 USDC en fixed-math 10^6 → 100_000_000.
    result = OrderResult(
        success=True,
        orderID="0xclob",
        status="matched",
        makingAmount="50000000",
        takingAmount="100000000",
        transactionsHashes=["0xtx"],
        errorMsg="",
    )
    clients = _stub_clients(order_result=result)
    await execute_order(
        _approved(),
        settings=_real_settings(),
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        alerts_queue=queue,
        **clients,
    )
    events = {queue.get_nowait().event for _ in range(queue.qsize())}
    assert "order_filled_large" in events


# --- success matched below threshold → pas d'alerte ------------------------


@pytest.mark.asyncio
async def test_small_fill_no_alert(
    session_factory: async_sessionmaker[AsyncSession],
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    del session_factory
    queue: asyncio.Queue[Alert] = asyncio.Queue()
    # takingAmount = 10 USDC (< 50 threshold).
    result = OrderResult(
        success=True,
        orderID="0xclob",
        status="matched",
        makingAmount="5000000",
        takingAmount="10000000",
        transactionsHashes=["0xtx"],
        errorMsg="",
    )
    clients = _stub_clients(order_result=result)
    await execute_order(
        _approved(),
        settings=_real_settings(),
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        alerts_queue=queue,
        **clients,
    )
    assert queue.empty()


# --- ExecutorAuthError dans orchestrator → push Alert(executor_auth_fatal) -


@pytest.mark.asyncio
async def test_orchestrator_pushes_auth_alert_on_fatal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(executor_orchestrator_module, "_QUEUE_GET_TIMEOUT_SECONDS", 0.02)
    fake_execute = AsyncMock(side_effect=ExecutorAuthError("invalid creds"))
    monkeypatch.setattr(executor_orchestrator_module, "execute_order", fake_execute)

    approved_queue: asyncio.Queue[OrderApproved] = asyncio.Queue()
    approved_queue.put_nowait(_approved())
    alerts_queue: asyncio.Queue[Alert] = asyncio.Queue()
    orch = ExecutorOrchestrator(
        session_factory,
        Settings(_env_file=None, dry_run=True),  # type: ignore[call-arg]
        approved_queue,
        alerts_queue=alerts_queue,
    )
    stop = asyncio.Event()
    with pytest.raises(ExecutorAuthError):
        await orch.run_forever(stop)
    assert stop.is_set()
    events = {alerts_queue.get_nowait().event for _ in range(alerts_queue.qsize())}
    assert "executor_auth_fatal" in events
