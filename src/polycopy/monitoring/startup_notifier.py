"""One-shot envoi du message de démarrage Telegram (M7 §8.2).

Interroge la DB pour les wallets pinned + dérive le statut des modules depuis
``settings``. Envoie 1 fois au boot et sort. No-op silencieux si
``TelegramClient.enabled is False``.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.dtos import ModuleStatus, PinnedWallet, StartupContext
from polycopy.monitoring.md_escape import wallet_short
from polycopy.monitoring.telegram_client import TelegramClient
from polycopy.storage.models import TargetTrader

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)


class StartupNotifier:
    """Construit + envoie en une passe le message de démarrage Telegram."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        telegram_client: TelegramClient,
        renderer: AlertRenderer,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._telegram = telegram_client
        self._renderer = renderer
        self._settings = settings

    async def send_once(self, stop_event: asyncio.Event) -> None:
        """Envoie le startup message une seule fois, puis retourne.

        Si ``stop_event`` est déjà set, on quitte sans envoyer (shutdown rapide).
        """
        if stop_event.is_set():
            return
        if not self._telegram.enabled:
            log.info("telegram_startup_skipped", reason="telegram_disabled")
            return
        try:
            ctx = await self._build_context()
            body = self._renderer.render_startup(ctx)
            ok = await self._telegram.send(body)
        except Exception:
            log.exception("telegram_startup_failed")
            return
        if ok:
            log.info("telegram_startup_sent", version=ctx.version, mode=ctx.mode)
        else:
            log.warning("telegram_startup_send_failed", version=ctx.version)

    async def _build_context(self) -> StartupContext:
        pinned = await self._load_pinned_wallets()
        modules = self._build_modules()
        dashboard_url = (
            f"http://{self._settings.dashboard_host}:{self._settings.dashboard_port}/"
            if self._settings.dashboard_enabled
            else None
        )
        mode = "dry-run" if self._settings.dry_run else "live"
        return StartupContext(
            version=_resolve_version(),
            mode=mode,
            boot_at=datetime.now(tz=UTC),
            pinned_wallets=pinned,
            modules=modules,
            dashboard_url=dashboard_url,
        )

    async def _load_pinned_wallets(self) -> list[PinnedWallet]:
        async with self._session_factory() as session:
            stmt = select(TargetTrader).where(TargetTrader.pinned.is_(True))
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return [
            PinnedWallet(wallet_short=wallet_short(row.wallet_address), label=row.label)
            for row in rows
        ]

    def _build_modules(self) -> list[ModuleStatus]:
        watcher_count = len(self._settings.target_wallets)
        modules: list[ModuleStatus] = [
            ModuleStatus(
                name="Watcher",
                enabled=True,
                detail=f"{watcher_count} wallets",
            ),
            ModuleStatus(
                name="Strategy",
                enabled=True,
                detail="filtres actifs",
            ),
            ModuleStatus(
                name="Executor",
                enabled=True,
                detail="simulé" if self._settings.dry_run else "live",
            ),
            ModuleStatus(
                name="Monitoring",
                enabled=True,
                detail=(
                    f"PnL {self._settings.pnl_snapshot_interval_seconds // 60} min, "
                    f"Telegram {'ON' if self._telegram.enabled else 'OFF'}"
                ),
            ),
            ModuleStatus(
                name="Dashboard",
                enabled=self._settings.dashboard_enabled,
                detail=(
                    f"{self._settings.dashboard_host}:{self._settings.dashboard_port}"
                    if self._settings.dashboard_enabled
                    else "désactivé"
                ),
            ),
            ModuleStatus(
                name="Discovery",
                enabled=self._settings.discovery_enabled,
                detail=(
                    (
                        f"{self._settings.discovery_interval_seconds // 3600} h cycle, "
                        f"{self._settings.scoring_version}"
                    )
                    if self._settings.discovery_enabled
                    else "désactivé"
                ),
            ),
        ]
        return modules


def _resolve_version() -> str:
    """Retourne ``"<pkg-version> (<git-sha>)"`` si git dispo, sinon juste la version."""
    try:
        pkg_version = importlib_metadata.version("polycopy")
    except importlib_metadata.PackageNotFoundError:
        pkg_version = "0.0.0"
    sha = _safe_git_sha()
    if sha:
        return f"{pkg_version} ({sha})"
    return pkg_version


def _safe_git_sha() -> str | None:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short=8", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None
