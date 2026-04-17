"""Tests du pipeline `execute_order` (dry-run + real path complet)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor.clob_metadata_client import ClobMetadataClient
from polycopy.executor.clob_write_client import ClobWriteClient
from polycopy.executor.dtos import (
    ExecutorAuthError,
    OrderResult,
    WalletState,
)
from polycopy.executor.pipeline import execute_order
from polycopy.executor.wallet_state_reader import WalletStateReader
from polycopy.storage.repositories import MyOrderRepository, MyPositionRepository
from polycopy.strategy.dtos import MarketMetadata, OrderApproved
from polycopy.strategy.gamma_client import GammaApiClient


def _dry_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=True,
        risk_available_capital_usd_stub=1000.0,
    )


def _real_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=False,
        polymarket_private_key="0x" + "1" * 64,
        polymarket_funder="0xF",
        risk_available_capital_usd_stub=1000.0,
    )


def _approved() -> OrderApproved:
    return OrderApproved(
        detected_trade_id=0,
        tx_hash="0xtx_exec",
        condition_id="0xcond",
        asset_id="123",
        side="BUY",
        my_size=10.0,
        my_price=0.0805,
    )


def _market(neg_risk: bool = False) -> MarketMetadata:
    return MarketMetadata(
        id="1",
        conditionId="0xcond",
        active=True,
        closed=False,
        archived=False,
        clobTokenIds='["123"]',
        outcomes='["Yes","No"]',
        negRisk=neg_risk,
    )


def _stub_clients(
    *,
    midpoint: float = 0.08,
    tick_size: float = 0.01,
    market: MarketMetadata | None = None,
    wallet: WalletState | None = None,
    order_result: OrderResult | None = None,
    sdk_exception: Exception | None = None,
) -> dict[str, Any]:
    metadata = AsyncMock(spec=ClobMetadataClient)
    metadata.get_tick_size.return_value = tick_size
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_market.return_value = market or _market()
    wallet_reader = AsyncMock(spec=WalletStateReader)
    wallet_reader.get_state.return_value = wallet or WalletState(
        total_position_value_usd=0.0,
        available_capital_usd=1000.0,
        open_positions_count=0,
    )
    write = MagicMock(spec=ClobWriteClient)
    if sdk_exception is not None:
        write.post_order = AsyncMock(side_effect=sdk_exception)
    elif order_result is not None:
        write.post_order = AsyncMock(return_value=order_result)
    return {
        "metadata_client": metadata,
        "gamma_client": gamma,
        "wallet_state_reader": wallet_reader,
        "write_client": write,
    }


# --- Dry-run path -----------------------------------------------------------


async def test_dryrun_simulates_no_post(
    session_factory: async_sessionmaker[AsyncSession],
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    del session_factory
    clients = _stub_clients()
    await execute_order(
        _approved(),
        settings=_dry_settings(),
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        **clients,
    )
    recent = await my_order_repo.list_recent(limit=1)
    assert recent[0].status == "SIMULATED"
    assert recent[0].simulated is True
    assert recent[0].price == 0.08  # arrondi à tick_size 0.01
    # write_client.post_order jamais appelé en dry-run
    clients["write_client"].post_order.assert_not_called()


# --- Real path : success matched -------------------------------------------


async def test_real_mode_filled_and_position_updated(
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    result = OrderResult(
        success=True,
        orderID="0xclob_1",
        status="matched",
        makingAmount="100000000",  # 100 USDC
        takingAmount="200000000",  # 200 shares
        transactionsHashes=["0xtx_chain"],
        errorMsg="",
    )
    clients = _stub_clients(order_result=result)
    await execute_order(
        _approved(),
        settings=_real_settings(),
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        **clients,
    )
    recent = await my_order_repo.list_recent(limit=1)
    assert recent[0].status == "FILLED"
    assert recent[0].clob_order_id == "0xclob_1"
    assert recent[0].transaction_hashes == ["0xtx_chain"]
    pos = await my_position_repo.get_open("0xcond")
    assert pos is not None
    assert pos.size == 200.0
    assert pos.avg_price == pytest.approx(0.5)  # 100 USDC / 200 shares


# --- Real path : capital exceeded ------------------------------------------


async def test_real_mode_capital_exceeded_rejects(
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    wallet = WalletState(
        total_position_value_usd=0.0,
        available_capital_usd=0.5,  # cost = 10*0.08 = 0.8 USD > 0.5 → reject
        open_positions_count=0,
    )
    clients = _stub_clients(wallet=wallet)
    await execute_order(
        _approved(),
        settings=_real_settings(),
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        **clients,
    )
    recent = await my_order_repo.list_recent(limit=1)
    assert recent[0].status == "REJECTED"
    assert recent[0].error_msg == "capital_exceeded_at_executor"
    clients["write_client"].post_order.assert_not_called()


# --- Real path : success=False -> REJECTED ----------------------------------


async def test_real_mode_validation_error_rejects(
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    result = OrderResult(
        success=False,
        errorMsg="not enough balance",
    )
    clients = _stub_clients(order_result=result)
    await execute_order(
        _approved(),
        settings=_real_settings(),
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        **clients,
    )
    recent = await my_order_repo.list_recent(limit=1)
    assert recent[0].status == "REJECTED"
    assert "not enough balance" in (recent[0].error_msg or "")


# --- Real path : auth error -> raises ExecutorAuthError ---------------------


async def test_real_mode_auth_error_raises(
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    result = OrderResult(
        success=False,
        errorMsg="Invalid api key",
    )
    clients = _stub_clients(order_result=result)
    with pytest.raises(ExecutorAuthError):
        await execute_order(
            _approved(),
            settings=_real_settings(),
            order_repo=my_order_repo,
            position_repo=my_position_repo,
            **clients,
        )
    recent = await my_order_repo.list_recent(limit=1)
    assert recent[0].status == "REJECTED"


# --- Real path : SDK raises exception -> FAILED -----------------------------


async def test_real_mode_sdk_exception_marks_failed(
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    clients = _stub_clients(sdk_exception=RuntimeError("network down"))
    await execute_order(
        _approved(),
        settings=_real_settings(),
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        **clients,
    )
    recent = await my_order_repo.list_recent(limit=1)
    assert recent[0].status == "FAILED"
    assert "network down" in (recent[0].error_msg or "")
