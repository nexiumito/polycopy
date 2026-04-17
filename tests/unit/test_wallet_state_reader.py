"""Tests du `WalletStateReader`."""

from typing import Any

import httpx
import pytest
import respx
import tenacity

from polycopy.config import Settings
from polycopy.executor.wallet_state_reader import WalletStateReader


def _dry_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=True,
        risk_available_capital_usd_stub=1234.0,
    )


def _real_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=False,
        polymarket_private_key="0x" + "1" * 64,
        polymarket_funder="0xFunder",
        risk_available_capital_usd_stub=1234.0,
    )


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        WalletStateReader._fetch_positions.retry,  # type: ignore[attr-defined]
        "wait",
        tenacity.wait_none(),
    )


async def test_dry_run_returns_stub_no_network() -> None:
    async with httpx.AsyncClient() as http:
        reader = WalletStateReader(http, _dry_settings())
        # Pas de respx.mock ici → si une requête sortait, ça raise.
        state = await reader.get_state()
    assert state.total_position_value_usd == 0.0
    assert state.available_capital_usd == 1234.0
    assert state.open_positions_count == 0


async def test_real_mode_sums_current_value(sample_positions: list[dict[str, Any]]) -> None:
    expected_total = sum(float(p.get("currentValue", 0) or 0) for p in sample_positions)
    with respx.mock(base_url="https://data-api.polymarket.com") as mock:
        mock.get("/positions").mock(return_value=httpx.Response(200, json=sample_positions))
        async with httpx.AsyncClient() as http:
            reader = WalletStateReader(http, _real_settings())
            state = await reader.get_state()
    assert state.total_position_value_usd == pytest.approx(expected_total)
    assert state.open_positions_count == len(sample_positions)
    assert state.available_capital_usd == 1234.0


async def test_real_mode_caches_within_ttl(sample_positions: list[dict[str, Any]]) -> None:
    with respx.mock(base_url="https://data-api.polymarket.com") as mock:
        route = mock.get("/positions").mock(
            return_value=httpx.Response(200, json=sample_positions),
        )
        async with httpx.AsyncClient() as http:
            reader = WalletStateReader(http, _real_settings())
            await reader.get_state()
            await reader.get_state()
            await reader.get_state()
    assert route.call_count == 1


async def test_real_mode_without_funder_raises() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=False,
        polymarket_private_key="0x" + "1" * 64,
        polymarket_funder=None,
    )
    async with httpx.AsyncClient() as http:
        reader = WalletStateReader(http, settings)
        with pytest.raises(RuntimeError, match="POLYMARKET_FUNDER"):
            await reader.get_state()
