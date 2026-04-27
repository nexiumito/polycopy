"""Test d'intégration : `getClobMarketInfo` V2 sur API réelle (M18 ME.6).

Smoke check que l'endpoint `GET /clob-markets/{condition_id}` retourne le
schéma documenté (`mts/mos/fd/t/...`). Aucun POST, aucune signature, ZERO fonds.

Pré-cutover : pointer sur `https://clob-v2.polymarket.com` via env override.
Post-cutover : default OK (`https://clob.polymarket.com` bascule sur V2).

Run via `pytest -m integration`.
"""

from __future__ import annotations

import os

import httpx
import pytest


@pytest.mark.integration
async def test_clob_v2_market_info_returns_documented_schema() -> None:
    """`/clob-markets/{condition_id}` retourne `cid`, `mts`, `mos`, `t`, etc.

    Smoke check qu'un marché récent (ANY conditionId actif sur Polymarket)
    expose la structure V2. Sélection d'un cid via `/sampling-markets` pour
    rester déterministe-friendly malgré la rotation des marchés.
    """
    host = os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
    async with httpx.AsyncClient(timeout=10.0) as http:
        sampling_resp = await http.get(f"{host}/sampling-markets")
        sampling_resp.raise_for_status()
        sampling = sampling_resp.json()
        assert isinstance(sampling, dict)
        markets = sampling.get("data") or []
        assert markets, "no sampling-markets data returned"
        condition_id = markets[0]["condition_id"]

        # Endpoint V2 (peut renvoyer 404 sur backend V1 pré-cutover —
        # explicitement skip dans ce cas pour éviter false-fail en transit).
        market_resp = await http.get(f"{host}/clob-markets/{condition_id}")
        if market_resp.status_code == 404:
            pytest.skip(
                f"/clob-markets/{condition_id} returned 404 — likely pre-cutover "
                "backend V1. Re-run post-cutover or with "
                "POLYMARKET_CLOB_HOST=https://clob-v2.polymarket.com",
            )
        market_resp.raise_for_status()
        market = market_resp.json()
        assert isinstance(market, dict)
        # Shape attendu V2 (single-letter keys) :
        # `c` (condition_id), `mts` (min tick size), `mos` (min order size),
        # `t` (tokens), `fd` (FeeDetails — optional, fee-free omits).
        cid_in_response = market.get("c") or market.get("cid")
        assert cid_in_response == condition_id
        assert "mts" in market, "missing minimum tick size (`mts`)"
        assert "t" in market, "missing tokens (`t`)"
        # `fd` (FeeDetails) est optionnel — fee-free markets l'omettent.
        fd = market.get("fd")
        if fd is not None:
            assert isinstance(fd, dict)
            assert "r" in fd, "fd present but missing rate (`r`)"
            assert "e" in fd, "fd present but missing exponent (`e`)"
