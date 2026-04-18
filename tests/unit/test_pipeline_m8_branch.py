"""Tests M8 §9.4 — branche pipeline ``_persist_realistic_simulated``.

Couvre :
- ``dry_run=true, realistic_fill=false`` → M3 path stub inchangé.
- ``dry_run=true, realistic_fill=true`` → fetch book + simulate + persist M8.
- ``dry_run=false, realistic_fill=true`` → flag ignoré, M3 live path.
- 4ᵉ garde-fou : ``_persist_realistic_simulated`` direct en live → AssertionError.
- Book accepted → MyOrder simulé + position virtuelle créée.
- Book rejected (insufficient_liquidity) → MyOrder REJECTED, **pas** de position.
- SELL sans position virtuelle → log warning, pas de crash.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor.clob_metadata_client import ClobMetadataClient
from polycopy.executor.clob_orderbook_reader import ClobOrderbookReader
from polycopy.executor.clob_write_client import ClobWriteClient
from polycopy.executor.dtos import Orderbook, OrderbookLevel, WalletState
from polycopy.executor.pipeline import _persist_realistic_simulated, execute_order
from polycopy.executor.wallet_state_reader import WalletStateReader
from polycopy.storage.repositories import MyOrderRepository, MyPositionRepository
from polycopy.strategy.dtos import MarketMetadata, OrderApproved
from polycopy.strategy.gamma_client import GammaApiClient


def _settings_m8(*, realistic: bool, dry: bool = True, partial: bool = False) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=dry,
        polymarket_private_key="0x" + "1" * 64 if not dry else None,
        polymarket_funder="0xF" if not dry else None,
        risk_available_capital_usd_stub=1000.0,
        dry_run_realistic_fill=realistic,
        dry_run_allow_partial_book=partial,
    )


def _approved(side: str = "BUY", size: float = 10.0) -> OrderApproved:
    return OrderApproved(
        detected_trade_id=0,
        tx_hash="0xtx_m8",
        condition_id="0xcond",
        asset_id="123",
        side=side,  # type: ignore[arg-type]
        my_size=size,
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


def _book(
    *,
    asks: list[tuple[str, str]] | None = None,
    bids: list[tuple[str, str]] | None = None,
) -> Orderbook:
    return Orderbook(
        asset_id="123",
        bids=[OrderbookLevel(price=Decimal(p), size=Decimal(s)) for p, s in (bids or [])],
        asks=[OrderbookLevel(price=Decimal(p), size=Decimal(s)) for p, s in (asks or [])],
        snapshot_at=datetime.now(tz=UTC),
    )


def _stub_clients(
    *,
    market: MarketMetadata | None = None,
    tick_size: float = 0.01,
    book: Orderbook | None = None,
) -> dict[str, Any]:
    metadata = AsyncMock(spec=ClobMetadataClient)
    metadata.get_tick_size.return_value = tick_size
    gamma = AsyncMock(spec=GammaApiClient)
    gamma.get_market.return_value = market or _market()
    wallet_reader = AsyncMock(spec=WalletStateReader)
    wallet_reader.get_state.return_value = WalletState(
        total_position_value_usd=0.0,
        available_capital_usd=1000.0,
        open_positions_count=0,
    )
    write = MagicMock(spec=ClobWriteClient)
    orderbook_reader = AsyncMock(spec=ClobOrderbookReader)
    if book is not None:
        orderbook_reader.get_orderbook.return_value = book
    return {
        "metadata_client": metadata,
        "gamma_client": gamma,
        "wallet_state_reader": wallet_reader,
        "write_client": write,
        "orderbook_reader": orderbook_reader,
    }


# --- Path branches ---------------------------------------------------------


async def test_dry_run_realistic_off_uses_m3_stub_path(
    session_factory: async_sessionmaker[AsyncSession],
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    del session_factory
    clients = _stub_clients()
    await execute_order(
        _approved(),
        settings=_settings_m8(realistic=False),
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        **clients,
    )
    recent = await my_order_repo.list_recent(limit=1)
    assert recent[0].status == "SIMULATED"
    assert recent[0].realistic_fill is False
    assert recent[0].simulated is True
    # Pas de fetch book.
    clients["orderbook_reader"].get_orderbook.assert_not_called()


async def test_dry_run_realistic_on_uses_m8_branch(
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    book = _book(asks=[("0.08", "50"), ("0.09", "60")])
    clients = _stub_clients(book=book)
    await execute_order(
        _approved(size=100.0),
        settings=_settings_m8(realistic=True),
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        **clients,
    )
    recent = await my_order_repo.list_recent(limit=1)
    assert recent[0].status == "SIMULATED"
    assert recent[0].realistic_fill is True
    assert recent[0].simulated is True
    assert recent[0].price == pytest.approx(0.085, abs=1e-9)
    # Position virtuelle créée
    open_virtual = await my_position_repo.list_open_virtual()
    assert len(open_virtual) == 1
    assert open_virtual[0].simulated is True
    assert open_virtual[0].size == 100.0


async def test_real_mode_ignores_realistic_flag(
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    """`DRY_RUN=false` → M3 live path strict, jamais de fetch /book."""
    from polycopy.executor.dtos import OrderResult

    clients = _stub_clients()
    clients["write_client"].post_order = AsyncMock(
        return_value=OrderResult(
            success=True,
            orderID="0xclob",
            status="matched",
            makingAmount="100000000",
            takingAmount="200000000",
            transactionsHashes=["0xtx"],
            errorMsg="",
        ),
    )
    await execute_order(
        _approved(),
        settings=_settings_m8(realistic=True, dry=False),
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        **clients,
    )
    clients["orderbook_reader"].get_orderbook.assert_not_called()
    recent = await my_order_repo.list_recent(limit=1)
    assert recent[0].status == "FILLED"
    assert recent[0].realistic_fill is False
    assert recent[0].simulated is False


async def test_book_insufficient_rejects_no_position(
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    book = _book(asks=[("0.08", "5")])
    clients = _stub_clients(book=book)
    await execute_order(
        _approved(size=100.0),
        settings=_settings_m8(realistic=True),
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        **clients,
    )
    recent = await my_order_repo.list_recent(limit=1)
    assert recent[0].status == "REJECTED"
    assert recent[0].error_msg == "insufficient_liquidity"
    assert recent[0].realistic_fill is True
    open_virtual = await my_position_repo.list_open_virtual()
    assert open_virtual == []


async def test_partial_book_allowed_creates_position(
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    book = _book(asks=[("0.08", "5")])
    clients = _stub_clients(book=book)
    await execute_order(
        _approved(size=100.0),
        settings=_settings_m8(realistic=True, partial=True),
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        **clients,
    )
    recent = await my_order_repo.list_recent(limit=1)
    assert recent[0].status == "SIMULATED"
    assert recent[0].size == pytest.approx(5.0)
    open_virtual = await my_position_repo.list_open_virtual()
    assert len(open_virtual) == 1


async def test_sell_without_virtual_position_logs_warning_no_crash(
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    book = _book(bids=[("0.07", "100")])
    clients = _stub_clients(book=book)
    await execute_order(
        _approved(side="SELL", size=10.0),
        settings=_settings_m8(realistic=True),
        order_repo=my_order_repo,
        position_repo=my_position_repo,
        **clients,
    )
    # Order persisté avec status SIMULATED quand même (audit trail).
    recent = await my_order_repo.list_recent(limit=1)
    assert recent[0].status == "SIMULATED"
    # Aucune position virtuelle créée.
    open_virtual = await my_position_repo.list_open_virtual()
    assert open_virtual == []


# --- 4ᵉ garde-fou ---------------------------------------------------------


async def test_4th_guardrail_assert_dry_run_true(
    my_order_repo: MyOrderRepository,
    my_position_repo: MyPositionRepository,
) -> None:
    """Calling _persist_realistic_simulated directly with dry_run=False must
    raise AssertionError (defense-in-depth invariant breach)."""
    import structlog

    book = _book(asks=[("0.08", "100")])
    orderbook_reader = AsyncMock(spec=ClobOrderbookReader)
    orderbook_reader.get_orderbook.return_value = book
    settings = _settings_m8(realistic=True, dry=False)
    with pytest.raises(AssertionError, match="must NEVER run in live mode"):
        await _persist_realistic_simulated(
            _approved(),
            tick_size=0.01,
            neg_risk=False,
            settings=settings,
            orderbook_reader=orderbook_reader,
            order_repo=my_order_repo,
            position_repo=my_position_repo,
            bound_log=structlog.get_logger(),
        )
