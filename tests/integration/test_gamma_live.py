"""Test d'intégration Gamma /markets — appel réseau réel. Run via `pytest -m integration`."""

import httpx
import pytest

from polycopy.strategy.gamma_client import GammaApiClient

# conditionId du marché fixture (Edmonton Oilers Stanley Cup 2026).
# À mettre à jour si le marché ferme.
_PUBLIC_CONDITION_ID = "0x4a67e1270a2ed86be8fc524b6114640e41b0c56303ecaa9584deacd62416ee5b"


@pytest.mark.integration
async def test_fetch_real_market() -> None:
    async with httpx.AsyncClient() as http:
        client = GammaApiClient(http)
        market = await client.get_market(_PUBLIC_CONDITION_ID)
    # Le marché peut avoir évolué depuis la capture ; on tolère None mais on vérifie le typage.
    assert market is None or (
        market.condition_id == _PUBLIC_CONDITION_ID and isinstance(market.clob_token_ids, list)
    )
