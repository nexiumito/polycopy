"""Tests du :class:`MarketCategoryResolver` — batching Gamma + cache (M12 §3.5).

Régression contre le bug 414 URI Too Long (cf. M12 post-release) : l'envoi
de >150 ``condition_ids`` dans une seule query string saturait le serveur
Gamma. Le resolver chunke désormais en lots de
:data:`_BATCH_SIZE_CONDITION_IDS` avec tolérance partial failure.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from polycopy.discovery.scoring.v2.category_resolver import (
    _BATCH_SIZE_CONDITION_IDS,
    MarketCategoryResolver,
)

_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


def _cid(i: int) -> str:
    """Produit un condition_id synthétique de 66 chars (``0x`` + 64 hex)."""
    return "0x" + f"{i:064x}"


def _market_row(cid: str, category: str) -> dict:
    """Réponse Gamma minimale pour 1 marché avec un tag matchant le set top-level."""
    return {"conditionId": cid, "tags": [{"label": category}]}


@respx.mock
async def test_empty_list_no_network_call() -> None:
    """``condition_ids=[]`` → ``{}``, zéro appel réseau."""
    route = respx.get(_GAMMA_MARKETS_URL).mock(
        return_value=httpx.Response(200, json=[]),
    )
    async with httpx.AsyncClient() as http:
        resolver = MarketCategoryResolver(http)
        result = await resolver.resolve_batch([])
    assert result == {}
    assert route.call_count == 0


@respx.mock
async def test_single_id_one_batch() -> None:
    """1 ID → 1 batch → catégorie retournée correctement."""
    cid = _cid(1)
    route = respx.get(_GAMMA_MARKETS_URL).mock(
        return_value=httpx.Response(200, json=[_market_row(cid, "Sports")]),
    )
    async with httpx.AsyncClient() as http:
        resolver = MarketCategoryResolver(http)
        result = await resolver.resolve_batch([cid])
    assert result == {cid: "Sports"}
    assert route.call_count == 1
    params = dict(route.calls[-1].request.url.params)
    assert params["condition_ids"] == cid
    assert params["include_tag"] == "true"


@respx.mock
@pytest.mark.parametrize(
    ("n_ids", "expected_batches"),
    [(49, 1), (50, 1), (51, 2)],
)
async def test_boundary_batch_sizes(n_ids: int, expected_batches: int) -> None:
    """Limites autour de :data:`_BATCH_SIZE_CONDITION_IDS`=50."""
    assert _BATCH_SIZE_CONDITION_IDS == 50  # garde-fou si la constante bouge

    cids = [_cid(i) for i in range(n_ids)]

    def handler(request: httpx.Request) -> httpx.Response:
        batch = request.url.params["condition_ids"].split(",")
        return httpx.Response(
            200,
            json=[_market_row(c, "Politics") for c in batch],
        )

    route = respx.get(_GAMMA_MARKETS_URL).mock(side_effect=handler)
    async with httpx.AsyncClient() as http:
        resolver = MarketCategoryResolver(http)
        result = await resolver.resolve_batch(cids)

    assert route.call_count == expected_batches
    assert len(result) == n_ids
    assert all(v == "Politics" for v in result.values())


@respx.mock
async def test_large_list_200_ids_four_batches() -> None:
    """200 IDs → 4 batches de 50 → dict de 200 clés, toutes résolues."""
    cids = [_cid(i) for i in range(200)]

    def handler(request: httpx.Request) -> httpx.Response:
        batch = request.url.params["condition_ids"].split(",")
        assert len(batch) <= _BATCH_SIZE_CONDITION_IDS
        return httpx.Response(
            200,
            json=[_market_row(c, "Crypto") for c in batch],
        )

    route = respx.get(_GAMMA_MARKETS_URL).mock(side_effect=handler)
    async with httpx.AsyncClient() as http:
        resolver = MarketCategoryResolver(http)
        result = await resolver.resolve_batch(cids)

    assert route.call_count == 4
    assert len(result) == 200
    assert set(result.keys()) == set(cids)
    assert all(v == "Crypto" for v in result.values())


@respx.mock
async def test_partial_failure_middle_batch_414(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Batch 1 OK, batch 2 → 414, batch 3 OK.

    Cids des batches 1 & 3 résolus, cids du batch 2 → ``_OTHER_CATEGORY``
    mais **pas cachés** (retry au prochain cycle).
    """
    cids = [_cid(i) for i in range(150)]  # 3 batches de 50
    batch2_cids = set(cids[50:100])

    call_index = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        idx = call_index["n"]
        call_index["n"] += 1
        batch = request.url.params["condition_ids"].split(",")
        if idx == 1:
            return httpx.Response(414)
        return httpx.Response(
            200,
            json=[_market_row(c, "Politics") for c in batch],
        )

    route = respx.get(_GAMMA_MARKETS_URL).mock(side_effect=handler)

    async with httpx.AsyncClient() as http:
        resolver = MarketCategoryResolver(http)
        result = await resolver.resolve_batch(cids)

        assert route.call_count == 3
        assert len(result) == 150
        # Batches 1 & 3 : catégorie propre.
        for cid in cids[:50] + cids[100:]:
            assert result[cid] == "Politics"
        # Batch 2 : fallback "other".
        for cid in cids[50:100]:
            assert result[cid] == "other"

        # Warning structlog émis pour batch_index=1 (rendu sur stdout via
        # ConsoleRenderer en test).
        captured = capsys.readouterr()
        assert "gamma_categories_batch_failed" in captured.out
        assert "batch_index=1" in captured.out

        # Les cids du batch en échec NE sont PAS en cache (retry au prochain cycle).
        for cid in batch2_cids:
            assert cid not in resolver._cache  # noqa: SLF001
        # Les cids des batches OK sont en cache.
        for cid in cids[:50] + cids[100:]:
            assert resolver._cache[cid] == "Politics"  # noqa: SLF001

        # Second appel : seul le batch 2 doit être re-fetché (50 IDs, 1 batch).
        # Remock proprement : désormais tout succède.
        route.reset()
        route.mock(
            side_effect=lambda request: httpx.Response(
                200,
                json=[
                    _market_row(c, "Sports") for c in request.url.params["condition_ids"].split(",")
                ],
            ),
        )
        result2 = await resolver.resolve_batch(cids)

    assert route.call_count == 1  # uniquement le batch précédemment failed
    # Les 100 déjà cachés gardent leur catégorie d'origine, les 50 recyclés → "Sports".
    for cid in cids[:50] + cids[100:]:
        assert result2[cid] == "Politics"
    for cid in cids[50:100]:
        assert result2[cid] == "Sports"


@respx.mock
async def test_cache_hit_skips_fetch() -> None:
    """50 cids en cache + 50 nouveaux → 1 seul appel réseau pour les 50 nouveaux."""
    cached_cids = [_cid(i) for i in range(50)]
    new_cids = [_cid(i) for i in range(50, 100)]

    def handler(request: httpx.Request) -> httpx.Response:
        batch = request.url.params["condition_ids"].split(",")
        return httpx.Response(
            200,
            json=[_market_row(c, "Tech") for c in batch],
        )

    route = respx.get(_GAMMA_MARKETS_URL).mock(side_effect=handler)

    async with httpx.AsyncClient() as http:
        resolver = MarketCategoryResolver(http)
        # Warm-up : cache les 50 premiers (1 batch).
        first = await resolver.resolve_batch(cached_cids)
        assert len(first) == 50
        assert route.call_count == 1

        # Mix cache + nouveaux : seul le batch des 50 nouveaux doit partir.
        mixed = await resolver.resolve_batch(cached_cids + new_cids)

    assert route.call_count == 2  # warm-up + 1 seul batch pour les nouveaux
    assert len(mixed) == 100
    assert all(mixed[c] == "Tech" for c in cached_cids + new_cids)
    # Le 2e appel réseau ne contient que les cids neufs (pas les cachés).
    second_call_batch = set(
        route.calls[1].request.url.params["condition_ids"].split(","),
    )
    assert second_call_batch == set(new_cids)
    assert second_call_batch.isdisjoint(set(cached_cids))
