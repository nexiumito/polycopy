"""Tests CandidatePool (assemblage holders + global trades + dédup + cap)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from polycopy.config import Settings
from polycopy.discovery.candidate_pool import CandidatePool
from polycopy.discovery.dtos import GlobalTrade, HolderEntry
from polycopy.strategy.dtos import MarketMetadata


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "target_wallets": "0xdummy",
        "discovery_candidate_pool_size": 100,
        "discovery_top_markets_for_holders": 2,
        "blacklisted_wallets": "",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _market(condition_id: str) -> MarketMetadata:
    return MarketMetadata(
        id="m1",
        conditionId=condition_id,
        active=True,
        closed=False,
    )


def _holder(wallet: str, amount: float = 100.0) -> HolderEntry:
    return HolderEntry(proxyWallet=wallet, amount=amount, outcomeIndex=0)


def _trade(wallet: str, size: float, price: float, condition: str = "0xmarket") -> GlobalTrade:
    return GlobalTrade(
        proxyWallet=wallet,
        asset="1",
        conditionId=condition,
        side="BUY",
        size=size,
        price=price,
        timestamp=1776504298,
        transactionHash="0xtx",
    )


async def test_build_merges_holders_and_trades_dedup() -> None:
    data_api = AsyncMock()
    data_api.get_holders = AsyncMock(
        side_effect=[
            [_holder("0xA"), _holder("0xB")],
            [_holder("0xB"), _holder("0xC")],  # B apparaît dans 2 marchés
        ],
    )
    data_api.get_global_trades = AsyncMock(
        return_value=[
            _trade("0xD", size=1000, price=0.5),  # 500 USD
            _trade("0xA", size=200, price=0.5),  # 100 USD (A déjà vu via holders)
        ],
    )
    gamma = AsyncMock()
    gamma.list_top_markets = AsyncMock(return_value=[_market("M1"), _market("M2")])

    pool = CandidatePool(data_api, gamma, None, _settings())
    candidates = await pool.build()

    addresses = {c.wallet_address for c in candidates}
    assert addresses == {"0xa", "0xb", "0xc", "0xd"}
    # 0xB vu dans 2 marchés → appearance=2 → signal supérieur à 0xC (appearance=1)
    ranked = {c.wallet_address: c.initial_signal for c in candidates}
    assert ranked["0xb"] > ranked["0xc"]


async def test_build_respects_blacklist() -> None:
    data_api = AsyncMock()
    data_api.get_holders = AsyncMock(return_value=[_holder("0xGOOD"), _holder("0xBAD")])
    data_api.get_global_trades = AsyncMock(return_value=[])
    gamma = AsyncMock()
    gamma.list_top_markets = AsyncMock(return_value=[_market("M1")])

    pool = CandidatePool(
        data_api,
        gamma,
        None,
        _settings(blacklisted_wallets="0xBAD,0xOTHER"),
    )
    candidates = await pool.build()
    assert {c.wallet_address for c in candidates} == {"0xgood"}


async def test_build_respects_explicit_excludes() -> None:
    """Les wallets `status='paused'` sont passés en `exclude_wallets`."""
    data_api = AsyncMock()
    data_api.get_holders = AsyncMock(
        return_value=[_holder("0xPAUSED"), _holder("0xFRESH")],
    )
    data_api.get_global_trades = AsyncMock(return_value=[])
    gamma = AsyncMock()
    gamma.list_top_markets = AsyncMock(return_value=[_market("M1")])

    pool = CandidatePool(data_api, gamma, None, _settings())
    candidates = await pool.build(exclude_wallets={"0xpaused"})
    assert {c.wallet_address for c in candidates} == {"0xfresh"}


async def test_build_caps_at_pool_size() -> None:
    """Si le pool distinct > pool_size, tronque par initial_signal DESC."""
    many_holders = [_holder(f"0x{i:040x}", amount=float(i)) for i in range(10)]
    data_api = AsyncMock()
    data_api.get_holders = AsyncMock(return_value=many_holders)
    data_api.get_global_trades = AsyncMock(return_value=[])
    gamma = AsyncMock()
    gamma.list_top_markets = AsyncMock(return_value=[_market("M1")])

    pool = CandidatePool(
        data_api,
        gamma,
        None,
        _settings(discovery_candidate_pool_size=3),
    )
    candidates = await pool.build()
    assert len(candidates) == 3
    # Les 3 premiers sont les plus gros (amount ∈ {7, 8, 9} → rank ∈ [1..3])
    top_signals = [c.initial_signal for c in candidates]
    assert top_signals == sorted(top_signals, reverse=True)


async def test_build_under_min_usd_filtered_out() -> None:
    """§14.5 #2 defense in depth : trades < 100 USD côté client filtrés aussi."""
    data_api = AsyncMock()
    data_api.get_holders = AsyncMock(return_value=[])
    data_api.get_global_trades = AsyncMock(
        return_value=[
            _trade("0xBIG", size=1000, price=0.5),  # 500 USD → OK
            _trade("0xSMALL", size=50, price=0.5),  # 25 USD → skip
        ],
    )
    gamma = AsyncMock()
    gamma.list_top_markets = AsyncMock(return_value=[_market("M1")])

    pool = CandidatePool(data_api, gamma, None, _settings())
    candidates = await pool.build()
    assert {c.wallet_address for c in candidates} == {"0xbig"}


async def test_build_with_goldsky_client_merges_source() -> None:
    from polycopy.discovery.dtos import GoldskyUserPosition

    data_api = AsyncMock()
    data_api.get_holders = AsyncMock(return_value=[])
    data_api.get_global_trades = AsyncMock(return_value=[])
    gamma = AsyncMock()
    gamma.list_top_markets = AsyncMock(return_value=[])

    goldsky = AsyncMock()
    goldsky.top_wallets_by_realized_pnl = AsyncMock(
        return_value=[
            GoldskyUserPosition(
                user="0xgs",
                tokenId="1",
                amount="0",
                avgPrice="500000",
                realizedPnl="1000000000",  # $1000 (10⁶ scale)
                totalBought="2000000000",
            ),
        ],
    )
    pool = CandidatePool(data_api, gamma, goldsky, _settings())
    candidates = await pool.build()
    assert len(candidates) == 1
    assert candidates[0].discovered_via == "goldsky"


async def test_build_goldsky_failure_does_not_crash() -> None:
    data_api = AsyncMock()
    data_api.get_holders = AsyncMock(return_value=[_holder("0xA")])
    data_api.get_global_trades = AsyncMock(return_value=[])
    gamma = AsyncMock()
    gamma.list_top_markets = AsyncMock(return_value=[_market("M1")])
    goldsky = AsyncMock()
    goldsky.top_wallets_by_realized_pnl = AsyncMock(
        side_effect=RuntimeError("subgraph down"),
    )
    pool = CandidatePool(data_api, gamma, goldsky, _settings())
    candidates = await pool.build()
    # Goldsky failed mais holders OK → on continue.
    assert {c.wallet_address for c in candidates} == {"0xa"}


pytestmark = pytest.mark.asyncio
