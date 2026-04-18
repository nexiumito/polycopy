#!/usr/bin/env python3
"""Seed synthétique de `trader_daily_pnl` pour tester scoring v2 en dev (M12).

**Usage dev uniquement** — bypasse la collecte naturelle 14j via
``TraderDailyPnlWriter`` en injectant des equity curves synthétiques pour
les wallets cibles. Permet de voir immédiatement les scores v2 non-nuls
dans le dashboard ``/traders/scoring`` sans attendre 2 semaines.

**Pas à utiliser en prod** : les curves synthétiques donnent des scores qui
ne reflètent **PAS** la vraie performance on-chain — seulement le plumbing
du code path v2.

Usage ::

    # Seed pour tous les TARGET_WALLETS + wallets `target_traders` existants.
    python scripts/seed_m12_dev_curves.py

    # Seed pour des wallets spécifiques.
    python scripts/seed_m12_dev_curves.py --wallets 0xabc,0xdef

    # Choisir le pattern de curve (default: mixed).
    python scripts/seed_m12_dev_curves.py --pattern growing
    python scripts/seed_m12_dev_curves.py --pattern volatile
    python scripts/seed_m12_dev_curves.py --pattern mixed

    # Nombre de jours d'historique synthétique (default: 30, min: 14).
    python scripts/seed_m12_dev_curves.py --days 60

Idempotent via ``TraderDailyPnlRepository.insert_if_new`` — re-run ne crée
pas de doublons (contrainte unique ``(wallet_address, date)``).
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from alembic.config import Config

from alembic import command
from polycopy.config import Settings
from polycopy.storage.dtos import TraderDailyPnlDTO
from polycopy.storage.engine import create_engine_and_session
from polycopy.storage.repositories import (
    TargetTraderRepository,
    TraderDailyPnlRepository,
)


def _ensure_migrations_applied(database_url: str) -> None:
    """Applique ``alembic upgrade head`` si la DB n'est pas à jour.

    Idempotent : no-op si déjà à head. Évite le piège d'une DB antérieure à M12
    qui n'aurait pas la table ``trader_daily_pnl`` (``init_db`` n'est appelé
    qu'au boot du bot, pas quand on lance un script utilitaire en standalone).
    """
    project_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(project_root / "alembic.ini"))
    # Convert async URL sqlite+aiosqlite:// → sync sqlite:// pour alembic.
    sync_url = database_url.replace("+aiosqlite", "")
    cfg.set_main_option("sqlalchemy.url", sync_url)
    command.upgrade(cfg, "head")

_CurvePattern = Literal["growing", "volatile", "mixed"]


def _growing_curve(days: int, start: float = 100.0) -> list[float]:
    """Croissance linéaire régulière. Sortino/Calmar saturés au sentinel 3.0."""
    return [start + i * 2.0 for i in range(days)]


def _volatile_curve(days: int, start: float = 100.0) -> list[float]:
    """Oscillation sinusoïdale + drift positif. Drawdowns observables."""
    curve: list[float] = []
    for i in range(days):
        drift = start + i * 1.0
        oscillation = 10.0 * math.sin(i * 0.5)
        curve.append(drift + oscillation)
    return curve


def _mixed_curve(
    days: int,
    wallet_hash: int,
    start: float = 100.0,
) -> list[float]:
    """Pattern déterministe basé sur hash wallet — diversifie le pool.

    Garantit que 2 wallets distincts ont des curves différentes (utile pour
    tester la winsorisation pool-wide et la normalisation).
    """
    phase = (wallet_hash % 7) * 0.3
    amplitude = 3.0 + (wallet_hash % 5)
    drift = 0.5 + (wallet_hash % 10) * 0.15
    curve: list[float] = []
    for i in range(days):
        value = start + i * drift + amplitude * math.sin(i * 0.4 + phase)
        curve.append(max(10.0, value))  # floor à 10 pour éviter equity négative
    return curve


def _build_curve(
    pattern: _CurvePattern,
    wallet: str,
    days: int,
) -> list[float]:
    if pattern == "growing":
        return _growing_curve(days)
    if pattern == "volatile":
        return _volatile_curve(days)
    # mixed : déterministe par wallet.
    wallet_hash = sum(ord(c) for c in wallet.lower())
    return _mixed_curve(days, wallet_hash)


async def _collect_target_wallets(
    settings: Settings,
    explicit: list[str] | None,
) -> list[str]:
    """Union ``TARGET_WALLETS`` env + ``target_traders`` DB + CLI ``--wallets``."""
    wallets: set[str] = set()
    if explicit:
        wallets.update(w.lower() for w in explicit)
    wallets.update(w.lower() for w in settings.target_wallets)
    engine, sf = create_engine_and_session(settings.database_url)
    try:
        target_repo = TargetTraderRepository(sf)
        for status in ("shadow", "active", "paused", "pinned"):
            rows = await target_repo.list_by_status(status)
            wallets.update(t.wallet_address for t in rows)
    finally:
        await engine.dispose()
    return sorted(wallets)


async def _seed(
    wallets: list[str],
    pattern: _CurvePattern,
    days: int,
    settings: Settings,
) -> dict[str, int]:
    """Seed l'equity curve pour chaque wallet. Retourne count inséré par wallet."""
    engine, sf = create_engine_and_session(settings.database_url)
    stats: dict[str, int] = {}
    try:
        repo = TraderDailyPnlRepository(sf)
        now = datetime.now(tz=UTC)
        for wallet in wallets:
            curve = _build_curve(pattern, wallet, days)
            inserted = 0
            previous = curve[0]
            for i, equity in enumerate(curve):
                snapshot_date = (now - timedelta(days=days - 1 - i)).date()
                realized_pnl_day = equity - previous if i > 0 else 0.0
                dto = TraderDailyPnlDTO(
                    wallet_address=wallet,
                    date=snapshot_date,
                    equity_usdc=equity,
                    realized_pnl_day=realized_pnl_day,
                    unrealized_pnl_day=0.0,
                    positions_count=3,
                )
                if await repo.insert_if_new(dto):
                    inserted += 1
                previous = equity
            stats[wallet] = inserted
    finally:
        await engine.dispose()
    return stats


async def _async_main(args: argparse.Namespace) -> int:
    if args.days < 14:
        print(
            "[FATAL] --days must be >= 14 (Sortino requires >= 14 curve points).",
            file=sys.stderr,
        )
        return 1
    settings = Settings()  # charge .env
    # S'assurer que la DB a bien la migration 0006 (évite l'erreur
    # "no such table: trader_daily_pnl" quand on lance le seed avant le bot).
    print(f"Ensuring DB migrations are up to date ({settings.database_url})...")
    _ensure_migrations_applied(settings.database_url)
    explicit_wallets: list[str] = []
    if args.wallets:
        explicit_wallets = [w.strip() for w in args.wallets.split(",") if w.strip()]
    wallets = await _collect_target_wallets(settings, explicit_wallets)
    if not wallets:
        print(
            "[FATAL] no wallets found (TARGET_WALLETS empty, target_traders empty, "
            "--wallets not provided).",
            file=sys.stderr,
        )
        return 1
    print(f"Seeding {len(wallets)} wallets with pattern={args.pattern}, days={args.days}")
    stats = await _seed(wallets, args.pattern, args.days, settings)
    total = sum(stats.values())
    for wallet, count in stats.items():
        suffix = " (already seeded, skipped)" if count == 0 else ""
        print(f"  {wallet}: +{count} rows{suffix}")
    print(f"✓ Total inserted: {total} rows")
    print(
        "Next: start the bot (python -m polycopy --verbose) and open "
        "http://127.0.0.1:8787/traders/scoring to see v2 scores.",
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed synthétique trader_daily_pnl pour tester scoring v2 en dev (M12)",
    )
    parser.add_argument(
        "--wallets",
        default=None,
        help=(
            "CSV de wallets à seed. Si absent, union de TARGET_WALLETS env + "
            "wallets `target_traders` existants."
        ),
    )
    parser.add_argument(
        "--pattern",
        default="mixed",
        choices=("growing", "volatile", "mixed"),
        help=(
            "Pattern d'equity curve. 'mixed' (default) = déterministe par wallet, "
            "diversifie pour tester winsorisation. 'growing' = saturé Sortino. "
            "'volatile' = drawdowns observables (Calmar pertinent)."
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Nombre de jours d'historique synthétique (min 14, default 30).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
