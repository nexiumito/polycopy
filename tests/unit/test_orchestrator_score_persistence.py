"""Tests non-régression Bug #3 + Bug #2 (J+3, 2026-05-02).

Bug #3 (CRITIQUE) : ``score_repo.insert(...)`` était dans un bloc
``if current is not None:`` → DB vide → 0 row dans ``trader_scores``,
``v2_scored=0`` partout dans les logs.

Bug #2 : ``compute_score()`` dispatchait via registry sur
``settings.scoring_version="v2.1"`` → wrapper v2 recevait un
``TraderMetrics`` v1 → warning systémique ``scoring_v2_wrong_metrics_type``.

Cf. [docs/todo.md §0bis](../../docs/todo.md) pour le récap audit.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.discovery.dtos import CandidateWallet, TraderMetrics
from polycopy.discovery.orchestrator import DiscoveryOrchestrator
from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2
from polycopy.monitoring.dtos import Alert
from polycopy.storage.repositories import (
    TargetTraderRepository,
    TraderEventRepository,
    TraderScoreRepository,
)


def _settings_v21(**overrides: Any) -> Settings:
    """Settings avec ``scoring_version="v2.1"`` (pilote v2.1) + valeurs par défaut tests.

    ``scoring_promotion_threshold=0.40`` pour que le score post-rank-normalize
    ~0.5-0.625 (pool homogène en mock) déclenche bien ``discovered_shadow``
    (sinon DecisionEngine retourne ``keep`` sans créer le target_trader, et
    on ne peut pas vérifier la persistance de trader_scores).
    """
    defaults: dict[str, Any] = {
        "target_wallets": "0xdummy",
        "discovery_enabled": True,
        "discovery_interval_seconds": 3600,
        "discovery_candidate_pool_size": 10,
        "discovery_backend": "data_api",
        "scoring_version": "v2.1",
        "scoring_promotion_threshold": 0.40,
        "scoring_demotion_threshold": 0.20,
        "scoring_min_closed_markets": 1,
        "trader_shadow_days": 7,
        "max_active_traders": 5,
        "scoring_v2_shadow_days": 0,
        "trader_daily_pnl_enabled": False,
        "scoring_v2_cold_start_mode": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _trader_metrics(wallet: str) -> TraderMetrics:
    """Stub TraderMetrics M5 minimal — accepté par scoring_min_closed_markets=1."""
    return TraderMetrics(
        wallet_address=wallet,
        resolved_positions_count=80,
        open_positions_count=2,
        win_rate=0.7,
        realized_roi=0.4,
        total_volume_usd=15_000.0,
        herfindahl_index=0.3,
        nb_distinct_markets=8,
        largest_position_value_usd=800.0,
        measurement_window_days=90,
        fetched_at=datetime.now(tz=UTC),
    )


def _trader_metrics_v2(wallet: str, *, passes_gates: bool = True) -> TraderMetricsV2:
    """Stub TraderMetricsV2 — passe les 7 gates si ``passes_gates=True``.

    cold_start_mode=True relâche days_active >=7 et trade_count >=20.
    Defaults choisis pour score post-rank-normalize ≥ 0.65 (promotion threshold)
    sur un pool d'1 wallet (rank=1.0 sur tous les facteurs).
    """
    return TraderMetricsV2(
        base=_trader_metrics(wallet),
        sortino_90d=1.5,
        calmar_90d=0.8,
        brier_90d=0.18,
        timing_alpha_weighted=0.55,
        hhi_categories=0.5,
        monthly_pnl_positive_ratio=0.66,
        zombie_ratio=0.1 if passes_gates else 0.5,  # >0.4 fail gate zombie
        sizing_cv=0.3,
        cash_pnl_90d=500.0 if passes_gates else -100.0,
        trade_count_90d=80,
        days_active=60 if passes_gates else 2,  # <7j fail gate days_active cold-start
        monthly_equity_curve=[100.0] * 30,
        net_exposure_ratio=0.8,
    )


@pytest_asyncio.fixture
async def _stop_after_one_cycle() -> asyncio.Event:
    return asyncio.Event()


async def _run_one_cycle(
    orch: DiscoveryOrchestrator,
    *,
    delay: float = 0.5,
) -> None:
    """Lance le cycle puis stop. Helper DRY."""
    stop = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(delay)
        stop.set()

    await asyncio.gather(orch.run_forever(stop), stop_soon())


async def test_orchestrator_writes_trader_scores_for_new_wallet(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bug #3 fix : DB vide, 1 candidat passant les gates v2.1 → 1 row trader_scores v2.1.

    Avant le fix, l'``score_repo.insert(...)`` était gardé par
    ``if current is not None`` → wallets nouveaux jamais persistés.
    """
    metrics_v1 = _trader_metrics("0xnew")
    metrics_v2 = _trader_metrics_v2("0xnew", passes_gates=True)

    with (
        patch("polycopy.discovery.orchestrator.CandidatePool", autospec=True) as mock_pool_cls,
        patch("polycopy.discovery.orchestrator.MetricsCollector", autospec=True) as mock_mc_cls,
        patch(
            "polycopy.discovery.orchestrator.MetricsCollectorV2",
            autospec=True,
        ) as mock_mc2_cls,
        patch(
            "polycopy.discovery.orchestrator.MarketCategoryResolver",
            autospec=True,
        ),
        patch(
            "polycopy.discovery.orchestrator.TraderDailyPnlWriter",
            autospec=True,
        ),
    ):
        mock_pool_cls.return_value.build = AsyncMock(
            return_value=[
                CandidateWallet(
                    wallet_address="0xnew",
                    discovered_via="holders",
                    initial_signal=2.0,
                ),
            ],
        )
        mock_mc_cls.return_value.collect = AsyncMock(return_value=metrics_v1)
        mock_mc2_cls.return_value.collect = AsyncMock(return_value=metrics_v2)

        alerts: asyncio.Queue[Alert] = asyncio.Queue(maxsize=50)
        orch = DiscoveryOrchestrator(
            session_factory,
            _settings_v21(trader_shadow_days=0, discovery_shadow_bypass=True),
            alerts,
        )
        await _run_one_cycle(orch)

    # 1) target_traders : le wallet doit exister.
    target_repo = TargetTraderRepository(session_factory)
    trader = await target_repo.get("0xnew")
    assert trader is not None, "wallet doit être inséré par DecisionEngine"

    # 2) trader_scores : 1 row v2.1 doit être écrite (avec target_trader_id valide).
    score_repo = TraderScoreRepository(session_factory)
    rows = await score_repo.list_for_wallet("0xnew")
    v21_rows = [r for r in rows if r.scoring_version == "v2.1"]
    assert len(v21_rows) == 1, f"attendu 1 row v2.1, observé {len(v21_rows)} (rows={rows})"
    assert v21_rows[0].target_trader_id == trader.id


async def test_orchestrator_v2_scored_count_includes_new_discoveries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bug #3 fix : ``v2_scored`` count == nb wallets passant les gates,

    quel que soit l'état initial de ``target_traders``. 3 candidats nouveaux
    qui passent les gates → 3 rows v2.1 + 0 row v1 (pilote v2.1, fix Bug #2).
    """
    candidates = [
        CandidateWallet(
            wallet_address=f"0xnew{i}",
            discovered_via="holders",
            initial_signal=2.0,
        )
        for i in range(3)
    ]

    async def fake_collect_v1(wallet: str) -> TraderMetrics:
        return _trader_metrics(wallet)

    async def fake_collect_v2(wallet: str) -> TraderMetricsV2:
        return _trader_metrics_v2(wallet, passes_gates=True)

    with (
        patch("polycopy.discovery.orchestrator.CandidatePool", autospec=True) as mock_pool_cls,
        patch("polycopy.discovery.orchestrator.MetricsCollector", autospec=True) as mock_mc_cls,
        patch(
            "polycopy.discovery.orchestrator.MetricsCollectorV2",
            autospec=True,
        ) as mock_mc2_cls,
        patch("polycopy.discovery.orchestrator.MarketCategoryResolver", autospec=True),
        patch("polycopy.discovery.orchestrator.TraderDailyPnlWriter", autospec=True),
    ):
        mock_pool_cls.return_value.build = AsyncMock(return_value=candidates)
        mock_mc_cls.return_value.collect = AsyncMock(side_effect=fake_collect_v1)
        mock_mc2_cls.return_value.collect = AsyncMock(side_effect=fake_collect_v2)

        alerts: asyncio.Queue[Alert] = asyncio.Queue(maxsize=50)
        orch = DiscoveryOrchestrator(
            session_factory,
            _settings_v21(trader_shadow_days=0, discovery_shadow_bypass=True),
            alerts,
        )
        await _run_one_cycle(orch)

    score_repo = TraderScoreRepository(session_factory)
    all_rows: list[Any] = []
    for c in candidates:
        all_rows.extend(await score_repo.list_for_wallet(c.wallet_address))
    v21_rows = [r for r in all_rows if r.scoring_version == "v2.1"]
    v1_rows = [r for r in all_rows if r.scoring_version == "v1"]
    assert len(v21_rows) == 3, f"attendu 3 rows v2.1, observé {len(v21_rows)}"
    # Bug #2 fix : v1 path skipped en pilote v2.1 → 0 row v1.
    assert len(v1_rows) == 0, f"v1 rows non attendues en pilote v2.1, observé {len(v1_rows)}"


async def test_orchestrator_no_score_row_on_gate_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Régression : un wallet rejeté gate ne doit PAS écrire de row trader_scores.

    Mais bien un trader_event ``gate_rejected`` (audit trail M12).
    """
    wallet = "0xgated"
    metrics_v1 = _trader_metrics(wallet)
    # passes_gates=False → days_active=2 < 7 cold-start → fail gate days_active_min.
    metrics_v2 = _trader_metrics_v2(wallet, passes_gates=False)

    with (
        patch("polycopy.discovery.orchestrator.CandidatePool", autospec=True) as mock_pool_cls,
        patch("polycopy.discovery.orchestrator.MetricsCollector", autospec=True) as mock_mc_cls,
        patch(
            "polycopy.discovery.orchestrator.MetricsCollectorV2",
            autospec=True,
        ) as mock_mc2_cls,
        patch("polycopy.discovery.orchestrator.MarketCategoryResolver", autospec=True),
        patch("polycopy.discovery.orchestrator.TraderDailyPnlWriter", autospec=True),
    ):
        mock_pool_cls.return_value.build = AsyncMock(
            return_value=[
                CandidateWallet(
                    wallet_address=wallet,
                    discovered_via="holders",
                    initial_signal=1.0,
                ),
            ],
        )
        mock_mc_cls.return_value.collect = AsyncMock(return_value=metrics_v1)
        mock_mc2_cls.return_value.collect = AsyncMock(return_value=metrics_v2)

        alerts: asyncio.Queue[Alert] = asyncio.Queue(maxsize=50)
        orch = DiscoveryOrchestrator(
            session_factory,
            _settings_v21(),
            alerts,
        )
        await _run_one_cycle(orch)

    # 1) trader_scores vide pour ce wallet (gate rejected = skip scoring complet).
    score_repo = TraderScoreRepository(session_factory)
    rows = await score_repo.list_for_wallet(wallet)
    assert rows == [], f"aucune row attendue, observé {rows}"

    # 2) trader_events doit contenir 1 row gate_rejected.
    event_repo = TraderEventRepository(session_factory)
    events = await event_repo.list_recent()
    rejected = [e for e in events if e.event_type == "gate_rejected"]
    assert len(rejected) == 1, f"attendu 1 gate_rejected event, observé {len(rejected)}"
