"""Test d'intégration CLOB /midpoint — appel réseau réel. Run via `pytest -m integration`."""

import httpx
import pytest

from polycopy.strategy.clob_read_client import ClobReadClient
from polycopy.strategy.gamma_client import GammaApiClient

_PUBLIC_CONDITION_ID = "0x4a67e1270a2ed86be8fc524b6114640e41b0c56303ecaa9584deacd62416ee5b"


@pytest.mark.integration
async def test_fetch_real_midpoint() -> None:
    async with httpx.AsyncClient() as http:
        gamma = GammaApiClient(http)
        market = await gamma.get_market(_PUBLIC_CONDITION_ID)
        if market is None or not market.clob_token_ids:
            pytest.skip("market unavailable for live test")
        clob = ClobReadClient(http)
        mid = await clob.get_midpoint(market.clob_token_ids[0])
    if mid is not None:
        assert 0.0 < mid < 1.0
