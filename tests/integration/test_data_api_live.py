"""Tests d'intégration : appels réseau réels Polymarket. Run via `pytest -m integration`."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from polycopy.watcher.data_api_client import DataApiClient

# Wallet public actif sur Polymarket (premier de TARGET_WALLETS du dev .env).
# À remplacer par une autre adresse vérifiée si celle-ci devient inactive.
_PUBLIC_WALLET = "0x19254b55e7c48e88baab9e62cc218223a6544654"


@pytest.mark.integration
async def test_fetch_real_wallet_activity() -> None:
    async with httpx.AsyncClient() as http:
        client = DataApiClient(http)
        trades = await client.get_trades(
            _PUBLIC_WALLET,
            since=datetime.now(tz=UTC) - timedelta(days=1),
        )
    assert isinstance(trades, list)
