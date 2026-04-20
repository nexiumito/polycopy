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
from typing import Literal

import structlog

from polycopy.cli.boot import build_orchestrators
from polycopy.cli.logging_config import configure_logging
from polycopy.cli.status_screen import (
    build_initial_module_status,
    render_crash_message,
    render_shutdown_message,
    render_status_screen,
)
from polycopy.cli.version import get_version
from polycopy.config import legacy_dry_run_detected, settings
from polycopy.monitoring.dtos import Alert
from polycopy.remote_control.sentinel import SentinelFile
from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.engine import create_engine_and_session
from polycopy.storage.init_db import init_db
from polycopy.storage.repositories import TradeLatencyRepository
from polycopy.strategy.dtos import OrderApproved

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
        help=(
            "[legacy] Force EXECUTION_MODE=dry_run. Préférer "
            "--execution-mode=dry_run ou la variable d'env EXECUTION_MODE."
        ),
    )
    parser.add_argument(
        "--execution-mode",
        choices=["simulation", "dry_run", "live"],
        default=None,
        help="Force le mode d'exécution (override EXECUTION_MODE env).",
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
        execution_mode=settings.execution_mode,
        targets=settings.target_wallets,
        version=get_version(),
    )
    engine, session_factory = create_engine_and_session(
        settings.database_url,
        echo=(settings.log_level.upper() == "DEBUG"),
    )
    try:
        await init_db(engine, session_factory, settings.target_wallets)

        # M11 : purge au boot des échantillons de latence obsolètes.
        if settings.latency_instrumentation_enabled:
            latency_repo = TradeLatencyRepository(session_factory)
            try:
                deleted = await latency_repo.purge_older_than(
                    days=settings.latency_sample_retention_days,
                )
                log.info(
                    "latency_purge_boot_completed",
                    deleted=deleted,
                    retention_days=settings.latency_sample_retention_days,
                )
            except Exception:
                log.exception("latency_purge_boot_error")

        detected_trades_queue: asyncio.Queue[DetectedTradeDTO] = asyncio.Queue(
            maxsize=_QUEUE_MAXSIZE,
        )
        approved_orders_queue: asyncio.Queue[OrderApproved] = asyncio.Queue(
            maxsize=_QUEUE_MAXSIZE,
        )
        alerts_queue: asyncio.Queue[Alert] = asyncio.Queue(maxsize=_ALERTS_QUEUE_MAXSIZE)
        stop_event = asyncio.Event()
        _install_signal_handlers(asyncio.get_running_loop(), stop_event)

        # M12_bis Phase D §4.2 : détection sentinel `~/.polycopy/halt.flag`
        # au boot → bifurcation running/paused. Le sentinel peut être posé
        # par `/stop` (Phase C), le kill switch M4 (Phase D commit #4), ou
        # l'auto-lockdown brute-force (Phase C).
        sentinel = SentinelFile(settings.remote_control_sentinel_path)
        boot_mode: Literal["normal", "paused"] = "paused" if sentinel.exists() else "normal"
        log.info(
            "polycopy_boot_mode",
            mode=boot_mode,
            halt_reason=sentinel.reason() if boot_mode == "paused" else None,
        )

        orchestrators = build_orchestrators(
            session_factory=session_factory,
            settings=settings,
            detected_trades_queue=detected_trades_queue,
            approved_orders_queue=approved_orders_queue,
            alerts_queue=alerts_queue,
            mode=boot_mode,
        )

        try:
            async with asyncio.TaskGroup() as tg:
                for orch in orchestrators:
                    tg.create_task(orch.run_forever(stop_event))
        except* asyncio.CancelledError:
            pass
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée CLI M9 / M10 (appelé par `__main__.py`)."""
    args = _parse_args(argv)
    # M10 : --execution-mode a priorité sur --dry-run legacy.
    if args.execution_mode is not None:
        settings.execution_mode = args.execution_mode
    elif args.dry_run:
        settings.execution_mode = "dry_run"
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
        skip_paths=settings.dashboard_log_skip_paths,
    )

    log = structlog.get_logger()

    # M10 : deprecation warning pour DRY_RUN env var legacy + --dry-run CLI flag.
    # Émis après configure_logging pour que le warning aille dans le fichier M9.
    if legacy_dry_run_detected():
        log.warning(
            "config_deprecation_dry_run_env",
            message=(
                "DRY_RUN is deprecated since M10. "
                "Use EXECUTION_MODE=simulation|dry_run|live instead. "
                "DRY_RUN will be removed in version+2."
            ),
            resolved_execution_mode=settings.execution_mode,
        )
    if args.dry_run and args.execution_mode is None:
        log.warning(
            "cli_deprecation_dry_run_flag",
            message="--dry-run is deprecated; use --execution-mode=dry_run",
        )

    # Écran rich uniquement si silent ET pas en mode daemon (--no-cli).
    show_rich_screen = silent and not args.no_cli
    if show_rich_screen:
        modules = build_initial_module_status(settings)
        render_status_screen(settings, modules, version=get_version())

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
