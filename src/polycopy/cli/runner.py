"""Runner CLI M9 — orchestre boot, logging, signal handlers et TaskGroup.

Diff strictement minimal vs `__main__.py` M8 : on déplace la logique
boot ici, on ajoute le rendu rich conditionnel + la config logging M9.
Aucune modification fonctionnelle des modules métier.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys

import structlog

from polycopy.cli.logging_config import configure_logging
from polycopy.cli.status_screen import (
    build_initial_module_status,
    render_crash_message,
    render_shutdown_message,
    render_status_screen,
)
from polycopy.cli.version import get_version
from polycopy.config import settings
from polycopy.dashboard.orchestrator import DashboardOrchestrator
from polycopy.discovery.orchestrator import DiscoveryOrchestrator
from polycopy.executor.orchestrator import ExecutorOrchestrator
from polycopy.monitoring.dtos import Alert
from polycopy.monitoring.orchestrator import MonitoringOrchestrator
from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.engine import create_engine_and_session
from polycopy.storage.init_db import init_db
from polycopy.strategy.dtos import OrderApproved
from polycopy.strategy.orchestrator import StrategyOrchestrator
from polycopy.watcher.orchestrator import WatcherOrchestrator

_QUEUE_MAXSIZE = 1000
_ALERTS_QUEUE_MAXSIZE = 100


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="polycopy",
        description="Polymarket copy trading bot",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force settings.dry_run=True (aucun ordre envoyé).",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override LOG_LEVEL from .env.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Restaure le mode M1..M8 : logs JSON streamés sur stdout en plus du fichier. "
            "Bypasse CLI_SILENT=true."
        ),
    )
    parser.add_argument(
        "--no-cli",
        action="store_true",
        help=(
            "Mode daemon : zéro stdout (pas d'écran rich, pas de stream JSON). "
            "Tous les logs vont uniquement vers LOG_FILE. Pour systemd / nohup."
        ),
    )
    return parser.parse_args(argv)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
) -> None:
    """Set `stop_event` au reçu de SIGINT/SIGTERM (no-op Windows ProactorLoop)."""
    log = structlog.get_logger()

    def _request_stop() -> None:
        if not stop_event.is_set():
            log.info("polycopy_stop_requested")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)


async def _async_main() -> None:
    """Logique boot asyncio identique à `__main__` M8 (zéro régression)."""
    log = structlog.get_logger()
    log.info(
        "polycopy_starting",
        dry_run=settings.dry_run,
        targets=settings.target_wallets,
        version=get_version(),
    )
    engine, session_factory = create_engine_and_session(
        settings.database_url,
        echo=(settings.log_level.upper() == "DEBUG"),
    )
    try:
        await init_db(engine, session_factory, settings.target_wallets)

        detected_trades_queue: asyncio.Queue[DetectedTradeDTO] = asyncio.Queue(
            maxsize=_QUEUE_MAXSIZE,
        )
        approved_orders_queue: asyncio.Queue[OrderApproved] = asyncio.Queue(
            maxsize=_QUEUE_MAXSIZE,
        )
        alerts_queue: asyncio.Queue[Alert] = asyncio.Queue(maxsize=_ALERTS_QUEUE_MAXSIZE)
        stop_event = asyncio.Event()
        _install_signal_handlers(asyncio.get_running_loop(), stop_event)

        watcher = WatcherOrchestrator(
            session_factory,
            settings,
            detected_trades_queue=detected_trades_queue,
            alerts_queue=alerts_queue,
        )
        strategy = StrategyOrchestrator(
            session_factory,
            settings,
            detected_trades_queue=detected_trades_queue,
            approved_orders_queue=approved_orders_queue,
            alerts_queue=alerts_queue,
        )
        # ExecutorOrchestrator lève RuntimeError si DRY_RUN=false sans clés
        # (instancié AVANT le TaskGroup pour propager l'erreur clairement).
        executor = ExecutorOrchestrator(
            session_factory,
            settings,
            approved_orders_queue=approved_orders_queue,
            alerts_queue=alerts_queue,
        )
        monitoring = MonitoringOrchestrator(session_factory, settings, alerts_queue)

        dashboard: DashboardOrchestrator | None = None
        if settings.dashboard_enabled:
            dashboard = DashboardOrchestrator(session_factory, settings)

        discovery: DiscoveryOrchestrator | None = None
        if settings.discovery_enabled:
            discovery = DiscoveryOrchestrator(session_factory, settings, alerts_queue)

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(watcher.run_forever(stop_event))
                tg.create_task(strategy.run_forever(stop_event))
                tg.create_task(executor.run_forever(stop_event))
                tg.create_task(monitoring.run_forever(stop_event))
                if dashboard is not None:
                    tg.create_task(dashboard.run_forever(stop_event))
                if discovery is not None:
                    tg.create_task(discovery.run_forever(stop_event))
        except* asyncio.CancelledError:
            pass
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée CLI M9 (appelé par `__main__.py`)."""
    args = _parse_args(argv)
    if args.dry_run:
        settings.dry_run = True
    if args.log_level is not None:
        settings.log_level = args.log_level

    # Mode silent : true si CLI_SILENT=true ET pas --verbose. --no-cli bypasse
    # aussi le stream stdout (mode daemon).
    silent = (settings.cli_silent and not args.verbose) or args.no_cli

    configure_logging(
        level=settings.log_level,
        log_file=settings.log_file,
        max_bytes=settings.log_file_max_bytes,
        backup_count=settings.log_file_backup_count,
        silent=silent,
    )

    # Écran rich uniquement si silent ET pas en mode daemon (--no-cli).
    show_rich_screen = silent and not args.no_cli
    if show_rich_screen:
        modules = build_initial_module_status(settings)
        render_status_screen(settings, modules, version=get_version())

    log = structlog.get_logger()
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        log.info("polycopy_stopped_by_user")
        if show_rich_screen:
            render_shutdown_message(settings)
        return 0
    except Exception as exc:
        log.exception("polycopy_crashed")
        if show_rich_screen:
            render_crash_message(settings, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
