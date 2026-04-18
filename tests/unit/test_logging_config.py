"""Tests M10 §4.2 + §8.3 — ordre des processors structlog.

L'invariant critique M10 : ``filter_noisy_endpoints`` est **en premier** dans
la chaîne, AVANT ``TimeStamper`` et ``JSONRenderer`` (économise le CPU de
formatage sur les events droppés).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from polycopy.cli.logging_config import configure_logging


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    """Préserve l'état global ``logging`` + ``structlog`` autour du test.

    ``configure_logging`` attache des handlers sur le root logger et reconfigure
    ``structlog``. Sans teardown explicite, la suite voisine (ex: tests httpx)
    hérite de ces handlers et de leur niveau → pollue les ``caplog`` d'autres
    tests. On restore à l'état initial après chaque test.
    """
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_structlog = structlog.get_config()
    try:
        yield
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)
        structlog.configure(**original_structlog)


def test_structlog_processor_filter_order(
    tmp_path: Path,
    _restore_logging: None,
) -> None:
    """Le 1er processor de la chaîne doit être ``filter_noisy_endpoints``."""
    del _restore_logging
    configure_logging(
        level="INFO",
        log_file=tmp_path / "polycopy.log",
        max_bytes=1_048_576,
        backup_count=1,
        silent=True,
    )
    config = structlog.get_config()
    processors = config["processors"]
    assert len(processors) > 1
    first = processors[0]
    # ``make_filter_noisy_endpoints`` retourne un closure ``_processor``.
    assert callable(first)
    assert first.__name__ == "_processor"
    # TimeStamper est APRÈS le filtre (économie CPU sur events droppés).
    names = [getattr(p, "__name__", type(p).__name__) for p in processors]
    idx_filter = names.index("_processor")
    idx_ts = next(
        (i for i, p in enumerate(processors) if isinstance(p, structlog.processors.TimeStamper)),
        -1,
    )
    assert idx_ts > idx_filter
