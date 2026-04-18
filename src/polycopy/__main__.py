"""Entrypoint CLI du bot polycopy.

Initialise le logging structuré JSON, la DB, et lance Watcher + Strategy en
parallèle dans un même `asyncio.TaskGroup` avec un `stop_event` partagé piloté
par les signaux SIGINT/SIGTERM.
"""

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

import structlog

from polycopy.config import settings
from polycopy.dashboard.orchestrator import DashboardOrchestrator
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


def _configure_logging(level: str) -> None:
    """Configure structlog en JSON sur stdout."""
    level_int = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(stream=sys.stdout, level=level_int, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        cache_logger_on_first_use=True,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="polycopy",
        description="Polymarket copy trading bot",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force settings.dry_run=True (no orders sent).",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override LOG_LEVEL from .env.",
    )
    return parser.parse_args()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """Set `stop_event` au reçu de SIGINT/SIGTERM (pas d'effet sous Windows ProactorLoop)."""
    log = structlog.get_logger()

    def _request_stop() -> None:
        if not stop_event.is_set():
            log.info("polycopy_stop_requested")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)


async def _run() -> None:
    log = structlog.get_logger()
    log.info(
        "polycopy_starting",
        dry_run=settings.dry_run,
        targets=settings.target_wallets,
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
        # ExecutorOrchestrator lève RuntimeError si DRY_RUN=false sans clés.
        # Volontairement instancié AVANT le TaskGroup pour que l'erreur propage clair.
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

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(watcher.run_forever(stop_event))
                tg.create_task(strategy.run_forever(stop_event))
                tg.create_task(executor.run_forever(stop_event))
                tg.create_task(monitoring.run_forever(stop_event))
                if dashboard is not None:
                    tg.create_task(dashboard.run_forever(stop_event))
        except* asyncio.CancelledError:
            pass
    finally:
        await engine.dispose()


def main() -> None:
    """Point d'entrée CLI."""
    args = _parse_args()
    if args.dry_run:
        settings.dry_run = True
    if args.log_level is not None:
        settings.log_level = args.log_level
    _configure_logging(settings.log_level)
    log = structlog.get_logger()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("polycopy_stopped_by_user")
        sys.exit(0)
    except Exception:
        log.exception("polycopy_crashed")
        sys.exit(1)


if __name__ == "__main__":
    main()
