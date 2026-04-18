"""Test M8 §9.12 — opt-in live ``ClobOrderbookReader.get_orderbook``.

Lance via ``pytest -m integration`` uniquement. Fetch dynamiquement un token
liquide via Gamma puis appelle ``/book`` réel pour s'assurer que le parser
tient face à la prod.
"""

from __future__ import annotations

import json

import httpx
import pytest

from polycopy.executor.clob_orderbook_reader import ClobOrderbookReader


@pytest.mark.integration
async def test_fetch_real_orderbook_top_market() -> None:
    async with httpx.AsyncClient(timeout=10.0) as http:
        # 1. Pick a top-liquidity market via Gamma.
        gamma_resp = await http.get(
            "https://gamma-api.polymarket.com/markets",
            params={
                "limit": 1,
                "order": "liquidityNum",
                "ascending": "false",
                "active": "true",
                "closed": "false",
            },
        )
        gamma_resp.raise_for_status()
        markets = gamma_resp.json()
        assert markets, "Gamma /markets returned empty"
        token_ids_raw = markets[0]["clobTokenIds"]
        token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else token_ids_raw
        assert token_ids, "no clob token ids on top market"
        token_id = token_ids[0]

        # 2. Hit /book via our reader.
        reader = ClobOrderbookReader(http, ttl_seconds=5)
        book = await reader.get_orderbook(token_id)
    assert len(book.asks) > 0 or len(book.bids) > 0
