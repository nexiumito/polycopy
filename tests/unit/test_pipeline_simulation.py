"""Tests M10 §3.6 + §8.3 — branche SIMULATION du pipeline executor.

Couvre :
- ``execution_mode="simulation"`` dispatch vers le stub M3 (pas de /book,
  pas de ``ClobWriteClient``).
- Order persisté avec ``simulated=True``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor.clob_metadata_client import ClobMetadataClient
from polycopy.executor.clob_orderbook_reader import ClobOrderbookReader
from polycopy.executor.clob_write_client import ClobWriteClient
from polycopy.executor.dtos import WalletState
from polycopy.executor.pipeline import execute_order
from polycopy.executor.wallet_state_reader import WalletStateReader
from polycopy.storage.repositories import MyOrderRepository, MyPositionRepository
from polycopy.strategy.dtos import MarketMetadata, OrderApproved
from polycopy.strategy.gamma_client import GammaApiClient


def _simulation_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        execution_mode="simulation",
        risk_available_capital_usd_stub=1000.0,
        dry_run_realistic_fill=True,  # ignoré en SIMULATION
    )


def _approved() -> OrderApproved:
    return OrderApproved(
        detected_trade_id=0,
        tx_hash="0xtx_sim",
        condition_id="0xcond",
        asset_id="123",
        side="BUY",
        my_size=10.0,
        my_price=0.5,
    )


async def test_simulation_mode_dispatches_to_stub(
    session_factory: async_sessionmaker[AsyncSession],
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    del session_factory
    metadata = AsyncMock(spec=ClobMetadataClient)
    metadata.get_tick_size.return_value = 0.01
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_market.return_value = MarketMetadata(
        id="1",
        conditionId="0xcond",
        active=True,
        closed=False,
        archived=False,
        clobTokenIds='["123"]',
        outcomes='["Yes","No"]',
        negRisk=False,
    )
    wallet_reader = AsyncMock(spec=WalletStateReader)
    wallet_reader.get_state.return_value = WalletState(
        total_position_value_usd=0.0,
        available_capital_usd=1000.0,
        open_positions_count=0,
    )
    write = MagicMock(spec=ClobWriteClient)
    orderbook_reader = AsyncMock(spec=ClobOrderbookReader)

    await execute_order(
        _approved(),
        settings=_simulation_settings(),
        metadata_client=metadata,
        gamma_client=gamma,
        wallet_state_reader=wallet_reader,
        write_client=write,
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        orderbook_reader=orderbook_reader,
    )

    recent = await my_order_repo.list_recent(limit=1)
    assert len(recent) == 1
    assert recent[0].status == "SIMULATED"
    assert recent[0].simulated is True
    assert recent[0].realistic_fill is False
    # SIMULATION n'utilise pas ClobWriteClient ni ClobOrderbookReader.
    orderbook_reader.get_orderbook.assert_not_called()
    write.post_order.assert_not_called()
    open_virtual = await my_position_repo.list_open_virtual()
    assert open_virtual == []
