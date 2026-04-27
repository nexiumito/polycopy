"""Test d'intégration : `FeeRateClient.get_fee_quote` V2 sur API réelle (M18 ME.6).

Smoke check que le client polycopy parse correctement le payload V2 contre
le backend Polymarket réel. Vérifie que sur un marché crypto fee-enabled,
`quote.rate > 0` ET `quote.exponent in (1, 2)` ; sur un marché fee-free,
`FeeQuote.zero()` est retourné.

Run via `pytest -m integration`.
"""

from __future__ import annotations

import os
from decimal import Decimal

import httpx
import pytest

from polycopy.config import Settings
from polycopy.executor.fee_rate_client import FeeQuote, FeeRateClient


@pytest.mark.integration
async def test_clob_v2_fee_rate_via_endpoint_real() -> None:
    """`get_fee_quote(asset_id, condition_id=cid)` sur 1 marché réel.

    Deux assertions safe :
    - Le call ne raise pas (réseau accessible, JSON valide).
    - Le quote retourné est soit ``FeeQuote.zero()`` (fee-free) soit
      ``rate > 0 ET exponent in (1, 2)`` (fee-enabled).
    """
    host = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        polymarket_clob_host=host,
    )
    async with httpx.AsyncClient(timeout=10.0) as http:
        # Pick a real market via `/sampling-markets`.
        sampling = (await http.get(f"{host}/sampling-markets")).json()
        markets = sampling.get("data") or []
        if not markets:
            pytest.skip("no sampling-markets returned by backend")
        market = markets[0]
        condition_id = market["condition_id"]
        # Premier token id (asset_id).
        tokens = market.get("tokens") or []
        token_id = tokens[0]["token_id"] if tokens else market.get("clobTokenIds", [None])[0]
        if not token_id:
            pytest.skip("no token_id resolvable from sampling-markets payload")

        client = FeeRateClient(http, settings=settings)
        # Skip propre si l'endpoint V2 n'est pas encore live (404 pré-cutover).
        try:
            quote = await client.get_fee_quote(token_id, condition_id=condition_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                pytest.skip(
                    f"/clob-markets/{condition_id} returned 404 — likely "
                    "pre-cutover backend V1. Re-run post-cutover."
                )
            raise

    if quote == FeeQuote.zero():
        # Fee-free market — schema cohérent.
        return
    assert quote.rate > Decimal("0")
    assert quote.exponent in (1, 2), f"unexpected exponent {quote.exponent}"
