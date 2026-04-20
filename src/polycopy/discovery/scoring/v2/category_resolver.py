"""Résolveur ``condition_id → catégorie top-level Polymarket`` (M12 §3.5).

Gamma ``/markets?include_tag=true`` retourne un array ``tags`` par marché
(~5.6 tags/marché, mix hiérarchique : ``"Israel"`` + ``"Geopolitics"`` +
``"Sports"`` + ``"2026 FIFA World Cup"``, etc.). Le flag ``forceShow=True``
est un marker UI carousel — **pas** une catégorie top-level (cf. analyse pré-code
+ ``docs/logbook_module/m12_notes.md``).

Stratégie retenue (décision D1 user 2026-04-18) :

1. Set hardcodé :data:`_TOP_LEVEL_POLYMARKET_CATEGORIES` de catégories stables
   observables dans la nav polymarket.com/markets.
2. Pour un marché, première ``tag.label`` matchant (case-sensitive) dans cette
   set = catégorie principale.
3. Fallback ``"other"`` si aucun match.
4. Override via env var ``SCORING_V2_CATEGORY_OVERRIDES`` (reportable v1.1 si
   besoin, non implémenté en v1 pour simplicité).

Cache in-memory 1-niveau : ``{condition_id: category_label}``. Pas de TTL car
la catégorie d'un marché ne change pas dans sa durée de vie active (< 1 an
typique). Invalidé uniquement au restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable

log = structlog.get_logger(__name__)


# Top-level categories Polymarket (décision D1). Observées via nav site +
# fixture gamma_markets_categories_sample.json (50 markets).
# Normalisation : "Economics" et "Economy" mappent au label canonique "Economy".
_TOP_LEVEL_POLYMARKET_CATEGORIES: frozenset[str] = frozenset(
    {
        "Politics",
        "Sports",
        "Crypto",
        "Economy",
        "Economics",
        "Geopolitics",
        "Tech",
        "Culture",
        "Pop Culture",
        "Climate",
        "Health",
        "Science",
        "Business",
        "Entertainment",
    },
)

# Alias → label canonique (cas "Economics" ↔ "Economy" + "Pop Culture" ↔
# "Culture" pour agréger les catégories proches dans le HHI).
_CATEGORY_CANONICAL: dict[str, str] = {
    "Economics": "Economy",
    "Economy": "Economy",
    "Pop Culture": "Culture",
    "Culture": "Culture",
}

_OTHER_CATEGORY = "other"

_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
_GAMMA_TIMEOUT = 15.0

# Taille max d'un batch envoyé à Gamma ``/markets?condition_ids=<csv>``. Chaque
# ``condition_id`` fait 66 chars (``0x`` + 64 hex) ; 50 IDs → URL ~3.3 KB, bien
# sous le seuil ~8 KB des serveurs HTTP standards (nginx default). Au-delà :
# 414 URI Too Long.
_BATCH_SIZE_CONDITION_IDS = 50


class MarketCategoryResolver:
    """Résout condition_id → catégorie top-level canonique.

    Cache in-memory, pas de TTL (catégories stables sur la durée de vie d'un
    marché). Appel ``/markets?condition_ids=<csv>&include_tag=true`` pour
    batch N markets inconnus.
    """

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client
        self._cache: dict[str, str] = {}

    async def resolve_batch(self, condition_ids: Iterable[str]) -> dict[str, str]:
        """Retourne ``{condition_id: category}`` pour chaque cid demandé.

        Les cids déjà en cache court-circuitent l'appel réseau. Les cids
        absents du cache sont fetchés en lots de :data:`_BATCH_SIZE_CONDITION_IDS`
        vers Gamma. Les cids appartenant à un batch en échec réseau (414,
        timeout…) reçoivent ``_OTHER_CATEGORY`` mais **ne sont pas cachés**,
        pour être re-tentés au cycle suivant.
        """
        cids = [c for c in condition_ids if c]
        if not cids:
            return {}
        # Cache lookup.
        result: dict[str, str] = {}
        missing: list[str] = []
        for cid in cids:
            if cid in self._cache:
                result[cid] = self._cache[cid]
            else:
                missing.append(cid)
        if not missing:
            return result
        fetched, failed_cids = await self._fetch_categories(missing)
        for cid in missing:
            if cid in failed_cids:
                # Batch réseau en échec : fallback mais pas de cache (retry
                # au prochain cycle).
                result[cid] = _OTHER_CATEGORY
                continue
            # Batch OK : soit Gamma a retourné une catégorie, soit le marché
            # n'existe pas côté Gamma (→ "other" légitime, cachable).
            category = fetched.get(cid, _OTHER_CATEGORY)
            self._cache[cid] = category
            result[cid] = category
        return result

    async def _fetch_categories(
        self,
        condition_ids: list[str],
    ) -> tuple[dict[str, str], set[str]]:
        """Batch fetch Gamma ``/markets?condition_ids=...&include_tag=true``.

        Les ``condition_ids`` sont chunkés sériellement en lots de
        :data:`_BATCH_SIZE_CONDITION_IDS` pour rester sous la limite d'URI
        serveur (~8 KB). Boucle sérielle (pas d'``asyncio.gather``) pour ne
        pas surcharger l'API publique Gamma.

        Partial failure toléré : si un batch échoue (414, 5xx, timeout…), il
        est loggé en ``warning`` et les autres batches continuent.

        Retourne un tuple ``(categories, failed_cids)`` :

        - ``categories`` : ``{conditionId: category}`` pour les marchés
          retournés par Gamma (batches OK uniquement).
        - ``failed_cids`` : ensemble des cids dont le batch a échoué
          réseau-côté. Le caller doit leur appliquer ``_OTHER_CATEGORY``
          sans les cacher (retry au prochain cycle).
        """
        if not condition_ids:
            return {}, set()
        out: dict[str, str] = {}
        failed: set[str] = set()
        for batch_index, start in enumerate(
            range(0, len(condition_ids), _BATCH_SIZE_CONDITION_IDS),
        ):
            batch = condition_ids[start : start + _BATCH_SIZE_CONDITION_IDS]
            batch_result = await self._fetch_categories_single_batch(batch, batch_index)
            if batch_result is None:
                failed.update(batch)
                continue
            out.update(batch_result)
        return out, failed

    async def _fetch_categories_single_batch(
        self,
        batch: list[str],
        batch_index: int,
    ) -> dict[str, str] | None:
        """Fetch un lot unique.

        Retourne ``None`` si le batch échoue (erreur HTTP/réseau) — le caller
        marquera tous les cids comme failed. Retourne un dict (possiblement
        vide) si le batch réussit.
        """
        try:
            response = await self._http.get(
                f"{_GAMMA_BASE_URL}/markets",
                params={
                    "condition_ids": ",".join(batch),
                    "include_tag": "true",
                },
                timeout=_GAMMA_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            log.warning(
                "gamma_categories_batch_failed",
                batch_index=batch_index,
                batch_size=len(batch),
                error=str(exc),
            )
            return None
        if not isinstance(data, list):
            log.warning(
                "gamma_categories_unexpected_payload_type",
                batch_index=batch_index,
                type=type(data).__name__,
            )
            return {}
        out: dict[str, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            cid = item.get("conditionId")
            if not isinstance(cid, str):
                continue
            tags_raw = item.get("tags", [])
            out[cid] = _pick_main_category(tags_raw)
        return out


def _pick_main_category(tags: Any) -> str:
    """Sélectionne la catégorie principale d'un marché depuis son array tags.

    - Premier ``tag.label`` matchant :data:`_TOP_LEVEL_POLYMARKET_CATEGORIES`
      = catégorie.
    - Alias normalisé via :data:`_CATEGORY_CANONICAL` (ex:
      ``"Economics" → "Economy"``).
    - Fallback ``"other"`` si aucun match.

    Pure function — testable isolément sur un array tags captured.
    """
    if not isinstance(tags, list):
        return _OTHER_CATEGORY
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        label = tag.get("label")
        if not isinstance(label, str):
            continue
        if label in _TOP_LEVEL_POLYMARKET_CATEGORIES:
            return _CATEGORY_CANONICAL.get(label, label)
    return _OTHER_CATEGORY
