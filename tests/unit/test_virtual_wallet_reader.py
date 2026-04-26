"""Tests M8 §9.5 — ``VirtualWalletStateReader``.

Couvre :
- 0 position virtuelle → total = virtual_capital, exposure=0, unrealized=0.
- 2 positions ouvertes + midpoints mockés → unrealized correct.
- 1 position close avec realized_pnl → somme dans total_usdc.
- midpoint 404 / exception → log warning + skip position.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.executor.virtual_wallet_reader import VirtualWalletStateReader
from polycopy.storage.repositories import MyPositionRepository
from polycopy.strategy.clob_read_client import ClobReadClient


def _settings(virtual_capital: float = 1000.0) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=True,
        dry_run_realistic_fill=True,
        dry_run_virtual_capital_usd=virtual_capital,
    )


async def test_no_positions_returns_capital_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clob_read = AsyncMock(spec=ClobReadClient)
    reader = VirtualWalletStateReader(session_factory, clob_read, _settings(2000.0))
    state = await reader.get_state()
    assert state.open_positions_count == 0
    assert state.total_position_value_usd == 0.0
    assert state.available_capital_usd == 2000.0


async def test_open_positions_unrealized_via_midpoint(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    await my_position_repo.upsert_virtual(
        condition_id="0xa",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    await my_position_repo.upsert_virtual(
        condition_id="0xb",
        asset_id="B",
        side="BUY",
        size_filled=20.0,
        fill_price=0.5,
    )
    clob_read = AsyncMock(spec=ClobReadClient)
    clob_read.get_midpoint.side_effect = lambda asset_id: {
        "A": 0.5,  # +0.10 unrealized vs avg 0.4 × 10 = +1.0
        "B": 0.45,  # -0.05 unrealized vs avg 0.5 × 20 = -1.0
    }[asset_id]
    reader = VirtualWalletStateReader(session_factory, clob_read, _settings())
    state = await reader.get_state()
    # exposure = 10*0.5 + 20*0.45 = 5 + 9 = 14
    assert state.total_position_value_usd == pytest.approx(14.0, abs=1e-9)
    # total = capital + realized (0) + unrealized (0)
    total = state.total_position_value_usd + state.available_capital_usd
    assert total == pytest.approx(1000.0, abs=1e-9)


async def test_realized_pnl_added_to_total(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    pos = await my_position_repo.upsert_virtual(
        condition_id="0xa",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    assert pos is not None
    await my_position_repo.close_virtual(pos.id, closed_at=datetime.now(tz=UTC), realized_pnl=15.0)
    clob_read = AsyncMock(spec=ClobReadClient)
    reader = VirtualWalletStateReader(session_factory, clob_read, _settings())
    state = await reader.get_state()
    total = state.total_position_value_usd + state.available_capital_usd
    assert total == pytest.approx(1015.0, abs=1e-9)


async def test_midpoint_failure_raises_midpoint_unavailable_no_last_known(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    """M17 MD.4 — fetch fail au tout 1ᵉʳ tick (last_known vide) → raise.

    Remplace l'ancien comportement M8 v1 (skip silencieux qui produisait
    un total_usdc=0 → drawdown factice — audit C-004). MD.4 : le writer
    upstream catch et skip le snapshot.
    """
    from polycopy.executor.exceptions import MidpointUnavailableError

    await my_position_repo.upsert_virtual(
        condition_id="0xa",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    clob_read = AsyncMock(spec=ClobReadClient)
    clob_read.get_midpoint.side_effect = httpx.HTTPError("boom")
    reader = VirtualWalletStateReader(session_factory, clob_read, _settings())
    with pytest.raises(MidpointUnavailableError) as exc:
        await reader.get_state()
    assert exc.value.asset_id == "A"
    assert exc.value.last_known_age_seconds is None


# --- M17 MD.4 : last_known_mid fallback (audit C-004) ----------------------


async def test_virtual_wallet_records_last_known_on_success(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    """1ᵉʳ tick OK → dict last_known peuplé."""
    await my_position_repo.upsert_virtual(
        condition_id="0xa",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    clob_read = AsyncMock(spec=ClobReadClient)
    clob_read.get_midpoint.return_value = 0.5
    reader = VirtualWalletStateReader(session_factory, clob_read, _settings())
    await reader.get_state()
    # Cache peuplé après le 1ᵉʳ fetch OK.
    assert "A" in reader._last_known_mid
    mid, _ = reader._last_known_mid["A"]
    assert mid == 0.5


async def test_virtual_wallet_uses_last_known_mid_on_transient_none(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    """Tick 1 OK populate cache → tick 2 fail HTTP → fallback last_known."""
    await my_position_repo.upsert_virtual(
        condition_id="0xa",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    clob_read = AsyncMock(spec=ClobReadClient)
    # Tick 1 : 0.5 ok, tick 2 : raise httpx.
    clob_read.get_midpoint.side_effect = [0.5, httpx.HTTPError("transient")]
    reader = VirtualWalletStateReader(session_factory, clob_read, _settings())
    state1 = await reader.get_state()
    assert state1.total_position_value_usd == pytest.approx(5.0)
    # Tick 2 : utilise last_known 0.5 → même valorisation, pas de raise.
    state2 = await reader.get_state()
    assert state2.total_position_value_usd == pytest.approx(5.0)


async def test_virtual_wallet_raises_on_mid_outage_exceeding_ttl(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """last_known stale > 600s → MidpointUnavailableError."""
    from polycopy.executor.exceptions import MidpointUnavailableError

    await my_position_repo.upsert_virtual(
        condition_id="0xa",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    clob_read = AsyncMock(spec=ClobReadClient)
    reader = VirtualWalletStateReader(session_factory, clob_read, _settings())
    # Pre-populate avec un timestamp ancien (TTL 600s écoulé).
    reader._last_known_mid["A"] = (
        0.5,
        datetime.now(tz=UTC) - timedelta(seconds=601),
    )
    clob_read.get_midpoint.side_effect = httpx.HTTPError("boom")
    with pytest.raises(MidpointUnavailableError) as exc:
        await reader.get_state()
    assert exc.value.asset_id == "A"
    assert exc.value.last_known_age_seconds is not None
    assert exc.value.last_known_age_seconds > 600.0


async def test_pnl_writer_skips_snapshot_on_midpoint_unavailable(
    session_factory: async_sessionmaker[AsyncSession],
    my_position_repo: MyPositionRepository,
) -> None:
    """PnlSnapshotWriter._tick catch MidpointUnavailableError → skip insert."""
    import asyncio

    from polycopy.monitoring.pnl_writer import PnlSnapshotWriter
    from polycopy.storage.repositories import PnlSnapshotRepository

    await my_position_repo.upsert_virtual(
        condition_id="0xa",
        asset_id="A",
        side="BUY",
        size_filled=10.0,
        fill_price=0.4,
    )
    clob_read = AsyncMock(spec=ClobReadClient)
    clob_read.get_midpoint.side_effect = httpx.HTTPError("boom")
    reader = VirtualWalletStateReader(session_factory, clob_read, _settings())
    alerts: asyncio.Queue = asyncio.Queue()
    writer = PnlSnapshotWriter(
        session_factory,
        _settings(),
        reader,
        alerts,
    )
    stop = asyncio.Event()
    # Appel direct du _tick pour vérifier le skip propre.
    await writer._tick(stop)
    # Aucun snapshot inséré.
    repo = PnlSnapshotRepository(session_factory)
    latest = await repo.get_latest()
    assert latest is None
