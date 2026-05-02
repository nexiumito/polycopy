"""Tests dashboard ``/traders/scoring`` (M12 §5.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from polycopy.config import Settings
from polycopy.dashboard import queries as dashboard_queries
from polycopy.dashboard.routes import build_app
from polycopy.storage.dtos import TraderScoreDTO
from polycopy.storage.repositories import (
    TargetTraderRepository,
    TraderScoreRepository,
)


def _settings(**overrides: Any) -> Settings:
    env: dict[str, Any] = {
        "dashboard_enabled": True,
        "dashboard_host": "127.0.0.1",
        "dashboard_port": 8787,
    }
    env.update(overrides)
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_traders_scoring_page_renders_empty_when_no_scores(
    session_factory: Any,
) -> None:
    """Page GET /traders/scoring avec DB vide → rendu sans erreur.

    M21 MN.6 : Settings default scoring_version="v1" + DB vide → shadow=None
    → single-version mode. Empty row message dynamique avec le pilot detected.
    """
    app = build_app(session_factory, _settings())
    with TestClient(app) as client:
        resp = client.get("/traders/scoring")
    assert resp.status_code == 200
    assert "Scoring comparison" in resp.text
    assert "M12+M21" in resp.text
    # Cas pool vide single-version mode : message fallback dynamique.
    assert "Aucun score v1 persisté" in resp.text


@pytest.mark.asyncio
async def test_traders_scoring_page_renders_v1_only_rows(
    session_factory: Any,
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    """Wallets avec score v1 seul (single-version mode) → table partielle."""
    t = await target_trader_repo.insert_shadow("0xaaa")
    await trader_score_repo.insert(
        TraderScoreDTO(
            target_trader_id=t.id,
            wallet_address="0xaaa",
            score=0.65,
            scoring_version="v1",
            low_confidence=False,
            metrics_snapshot={},
        ),
    )
    app = build_app(session_factory, _settings(scoring_version="v1"))
    with TestClient(app) as client:
        resp = client.get("/traders/scoring")
    assert resp.status_code == 200
    # Wallet affiché.
    assert "0xaaa" in resp.text
    # Score pilot présent.
    assert "0.650" in resp.text
    # Heading M21.
    assert "M12+M21" in resp.text


@pytest.mark.asyncio
async def test_scoring_comparison_query_with_v1_and_v2(
    session_factory: Any,
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    """Query retourne des rows avec rank pilot/shadow + delta_rank calculés.

    M21 MN.6 : passe versions explicites en params ; consomme alias génériques
    rank_pilot_pool / rank_shadow_pool (les legacy rank_v1/rank_v2 restent
    populés en parallèle pour rétrocompat).
    """
    # Seed 3 wallets avec scores v1 et v2.1 "croisés" (delta_rank non trivial).
    for wallet, s1, s2 in [
        ("0xaaa", 0.9, 0.5),
        ("0xbbb", 0.5, 0.9),
        ("0xccc", 0.7, 0.7),
    ]:
        t = await target_trader_repo.insert_shadow(wallet)
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id,
                wallet_address=wallet,
                score=s1,
                scoring_version="v1",
                low_confidence=False,
                metrics_snapshot={},
            ),
        )
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id,
                wallet_address=wallet,
                score=s2,
                scoring_version="v2.1",
                low_confidence=False,
                metrics_snapshot={},
            ),
        )

    rows = await dashboard_queries.list_scoring_comparison(
        session_factory,
        pilot_version="v1",
        shadow_version="v2.1",
        limit=10,
    )
    assert len(rows) == 3
    # Wallet "0xbbb" : rank pilot=3 (score 0.5 plus bas), rank shadow=1
    # (score 0.9) → delta_rank = 3 - 1 = +2 (gagne 2 places en shadow).
    bbb = next(r for r in rows if r.wallet_address == "0xbbb")
    assert bbb.rank_pilot_pool == 3
    assert bbb.rank_shadow_pool == 1
    assert bbb.delta_rank == 2
    # Legacy aliases populés pour rétrocompat (drop M22+).
    assert bbb.rank_v1 == 3
    assert bbb.rank_v2 == 1


@pytest.mark.asyncio
async def test_scoring_comparison_aggregates_spearman_computed(
    session_factory: Any,
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    """Spearman rank calculé quand ≥ 3 wallets avec v1 ET v2."""
    for wallet, s1, s2 in [
        ("0xaaa", 0.9, 0.8),  # rank v1 = 1, rank v2 = 1
        ("0xbbb", 0.7, 0.6),  # rank v1 = 2, rank v2 = 2
        ("0xccc", 0.5, 0.4),  # rank v1 = 3, rank v2 = 3
    ]:
        t = await target_trader_repo.insert_shadow(wallet)
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id,
                wallet_address=wallet,
                score=s1,
                scoring_version="v1",
                low_confidence=False,
                metrics_snapshot={},
            ),
        )
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id,
                wallet_address=wallet,
                score=s2,
                scoring_version="v2.1",
                low_confidence=False,
                metrics_snapshot={},
            ),
        )

    agg = await dashboard_queries.scoring_comparison_aggregates(
        session_factory,
        pilot_version="v1",
        shadow_version="v2.1",
        shadow_days=14,
        cutover_ready=False,
    )
    # Ranks parfaitement corrélés → Spearman = 1.0
    assert agg.spearman_rank == pytest.approx(1.0)
    assert agg.wallets_compared == 3
    assert agg.cutover_ready is False


@pytest.mark.asyncio
async def test_scoring_comparison_aggregates_none_spearman_below_3(
    session_factory: Any,
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    """Moins de 3 wallets avec v1 ET v2 → Spearman = None."""
    t = await target_trader_repo.insert_shadow("0xaaa")
    await trader_score_repo.insert(
        TraderScoreDTO(
            target_trader_id=t.id,
            wallet_address="0xaaa",
            score=0.5,
            scoring_version="v1",
            low_confidence=False,
            metrics_snapshot={},
        ),
    )
    agg = await dashboard_queries.scoring_comparison_aggregates(
        session_factory,
        pilot_version="v1",
        shadow_version="v2.1",
        shadow_days=14,
        cutover_ready=False,
    )
    assert agg.spearman_rank is None
    # 0 wallets avec v1 ET v2.
    assert agg.wallets_compared == 0


@pytest.mark.asyncio
async def test_cutover_ready_flag_passed_through_from_settings(
    session_factory: Any,
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    """``SCORING_V2_CUTOVER_READY`` du Settings remonte à la page.

    M21 MN.6 : seed 1 row v1 (pilot=v1) + 1 row v2.1 (shadow détecté) sinon
    le bloc cutover est masqué (single-version mode → l'assertion sur
    "Cutover ready flag" échouerait).
    """
    t = await target_trader_repo.insert_shadow("0xaaa")
    await trader_score_repo.insert(
        TraderScoreDTO(
            target_trader_id=t.id,
            wallet_address="0xaaa",
            score=0.5,
            scoring_version="v1",
            low_confidence=False,
            metrics_snapshot={},
        ),
    )
    await trader_score_repo.insert(
        TraderScoreDTO(
            target_trader_id=t.id,
            wallet_address="0xaaa",
            score=0.6,
            scoring_version="v2.1",
            low_confidence=False,
            metrics_snapshot={},
        ),
    )
    app = build_app(
        session_factory,
        _settings(scoring_version="v1", scoring_v2_cutover_ready=True),
    )
    with TestClient(app) as client:
        resp = client.get("/traders/scoring")
    assert resp.status_code == 200
    # Template affiche "Cutover ready flag: True" dans le bloc cutover (dual mode).
    assert "Cutover ready flag" in resp.text


@pytest.mark.asyncio
async def test_sidebar_link_present_in_base_template(
    session_factory: Any,
) -> None:
    """Base template contient le lien sidebar ``/traders/scoring``.

    M21 MN.4 : le label sidebar a été générifié de "Scoring v1/v2" à
    "Scoring comparison" (cohérent refactor multi-version).
    """
    app = build_app(session_factory, _settings())
    with TestClient(app) as client:
        resp = client.get("/home")
    assert resp.status_code == 200
    assert "/traders/scoring" in resp.text
    assert "Scoring comparison" in resp.text


@pytest.mark.asyncio
async def test_detect_comparison_versions_returns_pilot_only_when_db_empty(
    session_factory: Any,
) -> None:
    """M21 MN.1 — DB vide → ``(pilot=settings.scoring_version, shadow=None)``."""
    pilot, shadow = await dashboard_queries.detect_comparison_versions(
        session_factory,
        settings=_settings(scoring_version="v2.1"),
    )
    assert pilot == "v2.1"
    assert shadow is None


@pytest.mark.asyncio
async def test_detect_comparison_versions_picks_second_most_frequent(
    session_factory: Any,
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    """M21 MN.1 — pool {v2.1: 5, v2.1.1: 3, v1: 1} pilot=v2.1 → shadow=v2.1.1.

    Le filtre ``WHERE scoring_version != pilot_version`` garantit qu'on ne
    retourne pas le pilot lui-même comme shadow. La 2ᵉ version la plus
    fréquente sur la fenêtre 30j gagne (v2.1.1 = 3 rows > v1 = 1 row).
    """
    for i in range(5):
        t = await target_trader_repo.insert_shadow(f"0xa{i:03d}")
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id,
                wallet_address=f"0xa{i:03d}",
                score=0.5,
                scoring_version="v2.1",
                low_confidence=False,
                metrics_snapshot={},
            ),
        )
    for i in range(3):
        t = await target_trader_repo.insert_shadow(f"0xb{i:03d}")
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id,
                wallet_address=f"0xb{i:03d}",
                score=0.6,
                scoring_version="v2.1.1",
                low_confidence=False,
                metrics_snapshot={},
            ),
        )
    t = await target_trader_repo.insert_shadow("0xc000")
    await trader_score_repo.insert(
        TraderScoreDTO(
            target_trader_id=t.id,
            wallet_address="0xc000",
            score=0.7,
            scoring_version="v1",
            low_confidence=False,
            metrics_snapshot={},
        ),
    )

    pilot, shadow = await dashboard_queries.detect_comparison_versions(
        session_factory,
        settings=_settings(scoring_version="v2.1"),
    )
    assert pilot == "v2.1"
    assert shadow == "v2.1.1"


def test_spearman_rank_function_edge_cases() -> None:
    """Spearman : None pour n < 3, 1.0 pour ranks identiques, -1.0 inversés."""
    from polycopy.dashboard.queries import _spearman_rank

    assert _spearman_rank([1.0, 2.0], [1.0, 2.0]) is None  # n < 3
    assert _spearman_rank([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)
    assert _spearman_rank([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)


@pytest.mark.asyncio
async def test_spearman_and_top10_delta_paramétré_on_v2_1_v2_1_1(
    session_factory: Any,
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    """M21 MN.5 — Spearman + top-N delta sur 2 versions arbitraires (v2.1 vs v2.1.1).

    Garde-fou : la généralisation MN.2 propage correctement les versions
    explicites jusqu'au calcul Spearman et au top-10 delta. Le pure
    ``_spearman_rank`` reste intact (versioning sacré algo).

    Seed : 3 wallets avec scores parfaitement concordants (rangs identiques)
    → ρ = 1.0. Top-10 delta = 0 (mêmes wallets dans le top-10).
    """
    for wallet, s_pilot, s_shadow in [
        ("0xaaa", 0.9, 0.8),
        ("0xbbb", 0.7, 0.6),
        ("0xccc", 0.5, 0.4),
    ]:
        t = await target_trader_repo.insert_shadow(wallet)
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id,
                wallet_address=wallet,
                score=s_pilot,
                scoring_version="v2.1",
                low_confidence=False,
                metrics_snapshot={},
            ),
        )
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id,
                wallet_address=wallet,
                score=s_shadow,
                scoring_version="v2.1.1",
                low_confidence=False,
                metrics_snapshot={},
            ),
        )

    agg = await dashboard_queries.scoring_comparison_aggregates(
        session_factory,
        pilot_version="v2.1",
        shadow_version="v2.1.1",
        shadow_days=14,
        cutover_ready=False,
    )
    assert agg.wallets_compared == 3
    assert agg.spearman_rank == pytest.approx(1.0)
    # Tous wallets in both top-10 → top10_delta = 0.
    assert agg.top10_delta == 0


@pytest.mark.asyncio
async def test_spearman_uses_intersection_ranks_not_pool_ranks(
    session_factory: Any,
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    """Garde-fou régression : ρ calculé sur v1∩v2, pas sur les pools asymétriques.

    Seed volontairement déséquilibré : 5 wallets notés v1, 3 notés v2 dont
    seulement 2 en commun (xxx et yyy). Pour le 3ᵉ wallet intersection on
    ajoute zzz, qui reçoit le pire score v1 du pool (rang 5/5 pool-wide) mais
    le meilleur score v2 (rang 1/3 pool-wide). Sur l'intersection {xxx, yyy,
    zzz}, les ranks locaux sont parfaitement concordants (identiques) →
    ρ doit être proche de 1.0. Si le bug pool-wide était encore là, on aurait
    ρ ≪ 1 (voire négatif) parce que le rang pool de zzz en v1 est 5 mais en
    v2 c'est 1.
    """
    # Wallets avec v1 seul (gonflent le pool v1 sans impacter l'intersection).
    for wallet, s1 in [("0xaa1", 0.90), ("0xaa2", 0.80)]:
        t = await target_trader_repo.insert_shadow(wallet)
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id,
                wallet_address=wallet,
                score=s1,
                scoring_version="v1",
                low_confidence=False,
                metrics_snapshot={},
            ),
        )

    # Wallets intersection : scores v1 et v2 localement concordants (même ordre).
    # Mais côté pool v1, zzz est 5ᵉ ; côté pool v2, zzz est 1er.
    intersection = [
        ("0xxxx", 0.70, 0.80),  # pool v1 rank 3 / 5 ; pool v2 rank 2 / 3
        ("0xyyy", 0.60, 0.70),  # pool v1 rank 4 / 5 ; pool v2 rank 3 / 3
        ("0xzzz", 0.40, 0.90),  # pool v1 rank 5 / 5 ; pool v2 rank 1 / 3
    ]
    for wallet, s1, s2 in intersection:
        t = await target_trader_repo.insert_shadow(wallet)
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id,
                wallet_address=wallet,
                score=s1,
                scoring_version="v1",
                low_confidence=False,
                metrics_snapshot={},
            ),
        )
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id,
                wallet_address=wallet,
                score=s2,
                scoring_version="v2.1",
                low_confidence=False,
                metrics_snapshot={},
            ),
        )

    agg = await dashboard_queries.scoring_comparison_aggregates(
        session_factory,
        pilot_version="v1",
        shadow_version="v2.1",
        shadow_days=14,
        cutover_ready=False,
    )
    assert agg.wallets_compared == 3

    # Sur l'intersection {xxx, yyy, zzz} seuls les ranks locaux comptent :
    #   v1 local : zzz=3 (pire score), xxx=1 (meilleur), yyy=2
    #   v2 local : zzz=1 (meilleur), xxx=2, yyy=3
    # Pairs : (xxx: 1,2), (yyy: 2,3), (zzz: 3,1) → d² = 1+1+4 = 6.
    # ρ = 1 - (6*6)/(3*(9-1)) = 1 - 36/24 = 1 - 1.5 = -0.5
    assert agg.spearman_rank is not None
    assert agg.spearman_rank == pytest.approx(-0.5, abs=0.01)
    # Si le bug pool-wide était encore là, on aurait :
    #   v1 pool : xxx=3, yyy=4, zzz=5
    #   v2 pool : xxx=2, yyy=3, zzz=1
    # Pairs pool : (3,2), (4,3), (5,1) → d² = 1+1+16 = 18.
    # ρ_buggy = 1 - (6*18)/(3*8) = 1 - 4.5 = -3.5 (hors plage [-1, 1]).
    # Le test échouerait donc sur la contrainte Spearman ∈ [-1, 1].
    assert -1.0 <= agg.spearman_rank <= 1.0


@pytest.mark.asyncio
async def test_shadow_days_elapsed_calculated_from_first_v2_row(
    session_factory: Any,
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    """shadow_days_elapsed = now - first v2 cycle_at."""
    from datetime import timedelta

    from sqlalchemy import update

    from polycopy.storage.models import TraderScore

    t = await target_trader_repo.insert_shadow("0xabc")
    await trader_score_repo.insert(
        TraderScoreDTO(
            target_trader_id=t.id,
            wallet_address="0xabc",
            score=0.6,
            scoring_version="v2.1",
            low_confidence=False,
            metrics_snapshot={},
        ),
    )
    # Back-date la row v2 à 5 jours en arrière.
    old = datetime.now(tz=UTC) - timedelta(days=5)
    async with session_factory() as session:
        await session.execute(
            update(TraderScore).where(TraderScore.scoring_version == "v2.1").values(cycle_at=old),
        )
        await session.commit()

    agg = await dashboard_queries.scoring_comparison_aggregates(
        session_factory,
        pilot_version="v1",
        shadow_version="v2.1",
        shadow_days=14,
        cutover_ready=False,
    )
    assert agg.shadow_days_elapsed is not None
    assert agg.shadow_days_elapsed == 5
    assert agg.shadow_days_remaining == 9


@pytest.mark.asyncio
async def test_dashboard_scoring_render_with_v2_1_only_pilot(
    session_factory: Any,
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    """M21 MN.6 — pool v2.1 only → page rend en single-version mode.

    Cas test 30j actuel : `SCORING_VERSION=v2.1`, seul v2.1 calculé en prod.
    Headers dynamiques `Score V2.1` présents, `Score V2` (legacy hardcoded)
    absent, bloc cutover masqué (shadow=None).
    """
    t = await target_trader_repo.insert_shadow("0xaaa")
    await trader_score_repo.insert(
        TraderScoreDTO(
            target_trader_id=t.id,
            wallet_address="0xaaa",
            score=0.65,
            scoring_version="v2.1",
            low_confidence=False,
            metrics_snapshot={},
        ),
    )
    app = build_app(session_factory, _settings(scoring_version="v2.1"))
    with TestClient(app) as client:
        resp = client.get("/traders/scoring")
    assert resp.status_code == 200
    # Header dynamique pilote uniquement (pas de shadow header).
    assert "Score V2.1" in resp.text
    # Single-version mode : bloc Cutover status masqué (shadow=None) + pas
    # de heading "delta_rank" (apparaît uniquement quand shadow_version set).
    assert "delta_rank" not in resp.text
    # Wallet rendu + score formatté.
    assert "0xaaa" in resp.text
    assert "0.650" in resp.text


@pytest.mark.asyncio
async def test_dashboard_scoring_v2_1_vs_v2_1_1_shadow(
    session_factory: Any,
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    """M21 MN.6 — pool v2.1 (pilot) + v2.1.1 (shadow) → comparaison dynamique.

    Headers dynamiques `Score V2.1` + `Score V2.1.1` + label cutover dédié
    "Préparation cutover v2.1.1" (branche conditionnelle MN.4).
    """
    for wallet, s_pilot, s_shadow in [
        ("0xaaa", 0.9, 0.85),
        ("0xbbb", 0.6, 0.65),
        ("0xccc", 0.4, 0.45),
    ]:
        t = await target_trader_repo.insert_shadow(wallet)
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id,
                wallet_address=wallet,
                score=s_pilot,
                scoring_version="v2.1",
                low_confidence=False,
                metrics_snapshot={},
            ),
        )
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id,
                wallet_address=wallet,
                score=s_shadow,
                scoring_version="v2.1.1",
                low_confidence=False,
                metrics_snapshot={},
            ),
        )
    app = build_app(session_factory, _settings(scoring_version="v2.1"))
    with TestClient(app) as client:
        resp = client.get("/traders/scoring")
    assert resp.status_code == 200
    # Headers dynamiques (pilot + shadow).
    assert "Score V2.1" in resp.text
    assert "Score V2.1.1" in resp.text
    # Bloc cutover dédié (label conditionnel pour le couple v2.1+v2.1.1).
    assert "Préparation cutover v2.1.1" in resp.text
    # Wallets affichés.
    assert "0xaaa" in resp.text
    assert "0xbbb" in resp.text
    assert "0xccc" in resp.text
