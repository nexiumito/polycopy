"""Tests `TraderDailyPnlWriter` M12.

Contrats :

- Scanne les wallets ``status IN {shadow, active, paused, pinned}`` (ignore
  ``absent``).
- Idempotent sur re-run même jour (dédup via contrainte unique repo).
- ``equity_usdc`` = ``/value`` + cumul ``realized_pnl`` des positions résolues.
- ``positions_count`` = nombre de positions non résolues (ouvertes).
- Erreurs fetch un wallet n'arrêtent pas les autres.

Les tests mockent ``DiscoveryDataApiClient`` avec une classe stub async —
évite les round-trips HTTP tout en validant la logique d'agrégation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from polycopy.config import Settings
from polycopy.discovery.dtos import RawPosition
from polycopy.discovery.trader_daily_pnl_writer import TraderDailyPnlWriter
from polycopy.storage.repositories import (
    TargetTraderRepository,
    TraderDailyPnlRepository,
)


class _StubDataApi:
    """Stub async minimal pour isoler le writer des I/O HTTP."""

    def __init__(
        self,
        *,
        value_by_wallet: dict[str, float] | None = None,
        positions_by_wallet: dict[str, list[RawPosition]] | None = None,
        fail_on: set[str] | None = None,
    ) -> None:
        self._value = value_by_wallet or {}
        self._positions = positions_by_wallet or {}
        self._fail_on = fail_on or set()

    async def get_value(self, user: str) -> float:
        if user.lower() in self._fail_on:
            raise RuntimeError(f"value fetch failed for {user}")
        return float(self._value.get(user.lower(), 0.0))

    async def get_positions(self, user: str, **_: Any) -> list[RawPosition]:
        if user.lower() in self._fail_on:
            raise RuntimeError(f"positions fetch failed for {user}")
        return list(self._positions.get(user.lower(), []))


def _resolved_pos(realized: float) -> RawPosition:
    return RawPosition(
        conditionId="0xcid",
        asset="0xtoken",
        size=0.0,
        avgPrice=0.5,
        initialValue=100.0,
        currentValue=0.0,
        cashPnl=realized,
        realizedPnl=realized,
        totalBought=100.0,
        redeemable=True,
    )


def _open_pos(current_value: float) -> RawPosition:
    return RawPosition(
        conditionId="0xcid2",
        asset="0xtoken2",
        size=10.0,
        avgPrice=0.5,
        initialValue=100.0,
        currentValue=current_value,
        cashPnl=0.0,
        realizedPnl=0.0,
        totalBought=100.0,
        redeemable=False,
    )


def _settings_with_daily_pnl(**overrides: Any) -> Settings:
    """Instance de Settings acceptant override des 2 flags M12 foundations."""
    env: dict[str, Any] = {
        "trader_daily_pnl_enabled": True,
        "trader_daily_pnl_interval_seconds": 3600,
    }
    env.update(overrides)
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_snapshot_all_inserts_one_row_per_scannable_wallet(
    target_trader_repo: TargetTraderRepository,
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    # Seed 3 wallets : 1 shadow, 1 active (via transition), 1 paused.
    shadow = await target_trader_repo.insert_shadow("0xshadow")
    active = await target_trader_repo.insert_shadow("0xactive")
    paused = await target_trader_repo.insert_shadow("0xpaused")
    await target_trader_repo.transition_status("0xactive", new_status="active")
    await target_trader_repo.transition_status("0xpaused", new_status="paused")
    del shadow, active, paused

    api = _StubDataApi(
        value_by_wallet={"0xshadow": 50.0, "0xactive": 120.0, "0xpaused": 10.0},
        positions_by_wallet={
            "0xshadow": [_resolved_pos(5.0), _open_pos(30.0)],
            "0xactive": [_resolved_pos(25.0)],
            "0xpaused": [],
        },
    )
    writer = TraderDailyPnlWriter(
        data_api=api,  # type: ignore[arg-type]
        target_repo=target_trader_repo,
        daily_pnl_repo=trader_daily_pnl_repo,
        settings=_settings_with_daily_pnl(),
    )

    inserted = await writer._snapshot_all()
    assert inserted == 3

    shadow_curve = await trader_daily_pnl_repo.get_curve("0xshadow", days=2)
    assert len(shadow_curve) == 1
    # equity = value (50) + cumul realized (5) = 55.0
    assert float(shadow_curve[0].equity_usdc) == pytest.approx(55.0)
    # 1 position non résolue
    assert shadow_curve[0].positions_count == 1


@pytest.mark.asyncio
async def test_snapshot_all_is_idempotent_same_day(
    target_trader_repo: TargetTraderRepository,
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    await target_trader_repo.insert_shadow("0xabc")
    api = _StubDataApi(
        value_by_wallet={"0xabc": 100.0},
        positions_by_wallet={"0xabc": []},
    )
    writer = TraderDailyPnlWriter(
        data_api=api,  # type: ignore[arg-type]
        target_repo=target_trader_repo,
        daily_pnl_repo=trader_daily_pnl_repo,
        settings=_settings_with_daily_pnl(),
    )
    first = await writer._snapshot_all()
    second = await writer._snapshot_all()
    assert first == 1
    assert second == 0
    # Une seule row en DB
    curve = await trader_daily_pnl_repo.get_curve("0xabc", days=2)
    assert len(curve) == 1


@pytest.mark.asyncio
async def test_snapshot_all_skips_absent_wallets(
    target_trader_repo: TargetTraderRepository,
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    # Aucun wallet seeded → la DB est vide mais le scan doit passer sans erreur.
    api = _StubDataApi()
    writer = TraderDailyPnlWriter(
        data_api=api,  # type: ignore[arg-type]
        target_repo=target_trader_repo,
        daily_pnl_repo=trader_daily_pnl_repo,
        settings=_settings_with_daily_pnl(),
    )
    assert await writer._snapshot_all() == 0


@pytest.mark.asyncio
async def test_snapshot_all_continues_on_wallet_fetch_error(
    target_trader_repo: TargetTraderRepository,
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    """Erreur fetch sur un wallet ne bloque pas le snapshot des autres."""
    await target_trader_repo.insert_shadow("0xok")
    await target_trader_repo.insert_shadow("0xfails")
    api = _StubDataApi(
        value_by_wallet={"0xok": 10.0},
        positions_by_wallet={"0xok": []},
        fail_on={"0xfails"},
    )
    writer = TraderDailyPnlWriter(
        data_api=api,  # type: ignore[arg-type]
        target_repo=target_trader_repo,
        daily_pnl_repo=trader_daily_pnl_repo,
        settings=_settings_with_daily_pnl(),
    )
    inserted = await writer._snapshot_all()
    assert inserted == 1
    assert len(await trader_daily_pnl_repo.get_curve("0xok", days=2)) == 1
    assert len(await trader_daily_pnl_repo.get_curve("0xfails", days=2)) == 0


@pytest.mark.asyncio
async def test_run_forever_exits_on_stop_event(
    target_trader_repo: TargetTraderRepository,
    trader_daily_pnl_repo: TraderDailyPnlRepository,
) -> None:
    """stop_event.set() → exit rapide de la boucle (<1s)."""
    api = _StubDataApi()
    writer = TraderDailyPnlWriter(
        data_api=api,  # type: ignore[arg-type]
        target_repo=target_trader_repo,
        daily_pnl_repo=trader_daily_pnl_repo,
        settings=_settings_with_daily_pnl(trader_daily_pnl_interval_seconds=3600),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(writer.run_forever(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
