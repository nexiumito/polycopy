"""Tests GoldskyClient (respx mocks)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from polycopy.discovery.goldsky_client import GoldskyClient, GoldskyError

_FIXTURES = Path(__file__).parent.parent / "fixtures"


class _FakeSettings:
    goldsky_positions_subgraph_url = (
        "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/"
        "subgraphs/pnl-subgraph/0.0.14/gn"
    )


@pytest.fixture
def settings() -> _FakeSettings:
    return _FakeSettings()


@pytest.fixture
def goldsky_payload() -> dict:
    return json.loads((_FIXTURES / "goldsky_positions_topn_sample.json").read_text())


@respx.mock
async def test_top_wallets_parses_user_positions(
    goldsky_payload: dict,
    settings: _FakeSettings,
) -> None:
    respx.post(settings.goldsky_positions_subgraph_url).mock(
        return_value=httpx.Response(200, json=goldsky_payload),
    )
    async with httpx.AsyncClient() as http:
        client = GoldskyClient(http, settings)  # type: ignore[arg-type]
        positions = await client.top_wallets_by_realized_pnl(first=5)
    assert len(positions) == 5
    assert all(p.user.startswith("0x") for p in positions)
    # BigInt values parsent bien en string.
    assert all(isinstance(p.realized_pnl, str) for p in positions)


@respx.mock
async def test_top_wallets_raises_on_graphql_errors(
    settings: _FakeSettings,
) -> None:
    err_response = {
        "errors": [
            {"message": "Type `Query` has no field `userPositions`"},
        ],
    }
    respx.post(settings.goldsky_positions_subgraph_url).mock(
        return_value=httpx.Response(200, json=err_response),
    )
    async with httpx.AsyncClient() as http:
        client = GoldskyClient(http, settings)  # type: ignore[arg-type]
        with pytest.raises(GoldskyError, match="goldsky_query_errors"):
            await client.top_wallets_by_realized_pnl()


@respx.mock
async def test_top_wallets_raises_on_weird_shape(
    settings: _FakeSettings,
) -> None:
    respx.post(settings.goldsky_positions_subgraph_url).mock(
        return_value=httpx.Response(200, json={"data": {"userPositions": "not-a-list"}}),
    )
    async with httpx.AsyncClient() as http:
        client = GoldskyClient(http, settings)  # type: ignore[arg-type]
        with pytest.raises(GoldskyError, match="unexpected userPositions"):
            await client.top_wallets_by_realized_pnl()


@respx.mock
async def test_top_wallets_empty_data_returns_empty_list(
    settings: _FakeSettings,
) -> None:
    respx.post(settings.goldsky_positions_subgraph_url).mock(
        return_value=httpx.Response(200, json={"data": {"userPositions": []}}),
    )
    async with httpx.AsyncClient() as http:
        client = GoldskyClient(http, settings)  # type: ignore[arg-type]
        positions = await client.top_wallets_by_realized_pnl()
    assert positions == []
