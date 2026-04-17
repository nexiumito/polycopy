"""Entrypoint CLI du bot polycopy.

Initialise le logging structuré JSON, la DB, puis lance `WatcherOrchestrator`.
"""

import argparse
import asyncio
import logging
import sys

import structlog

from polycopy.config import settings
from polycopy.storage.engine import create_engine_and_session
from polycopy.storage.init_db import init_db
from polycopy.watcher.orchestrator import WatcherOrchestrator


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
        await WatcherOrchestrator(session_factory, settings).run_forever()
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
