"""Entrypoint du bot. Orchestre les coroutines async."""

import asyncio
import sys

import structlog

from polycopy.config import settings

log = structlog.get_logger()


async def run() -> None:
    """Lance les workers : watcher, strategy, executor, monitoring."""
    log.info("polycopy_starting", dry_run=settings.dry_run, targets=settings.target_wallets)

    # TODO M1 : démarrer le Watcher
    # TODO M2 : démarrer la Strategy
    # TODO M3 : démarrer l'Executor
    # TODO M4 : démarrer le Monitoring

    log.warning("not_implemented_yet", message="Implémenter les modules M1-M4")
    await asyncio.sleep(1)


def main() -> None:
    """Point d'entrée CLI."""
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("polycopy_stopped_by_user")
        sys.exit(0)


if __name__ == "__main__":
    main()
