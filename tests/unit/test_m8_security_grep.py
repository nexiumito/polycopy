"""Tests M8 — aucun secret hardcodé dans les sources M8.

Grep automatisé : aucun nom de variable d'env sensible ne doit apparaître en
clair dans les modules M8 (en dehors des refs documentaires / spec). Cohérent
avec la discipline ``M5`` + ``M7`` (cf. CLAUDE.md sécurité).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_FORBIDDEN_PATTERNS = [
    r"POLYMARKET_PRIVATE_KEY",
    r"POLYMARKET_FUNDER",
    r"TELEGRAM_BOT_TOKEN",
    r"GOLDSKY_API_KEY",
    # CLOB L2 creds individuels (api_key/api_secret/api_passphrase peuvent
    # apparaître dans les attributs Pydantic legitimes — on cible les
    # combinaisons de mots typiques d'un dump). Les modules M8 ne touchent
    # pas aux creds donc on assure aucune mention.
    r"api_passphrase",
]


_M8_FILES = [
    Path("src/polycopy/executor/clob_orderbook_reader.py"),
    Path("src/polycopy/executor/realistic_fill.py"),
    Path("src/polycopy/executor/virtual_wallet_reader.py"),
    Path("src/polycopy/executor/dry_run_resolution_watcher.py"),
]


@pytest.mark.parametrize("path", _M8_FILES)
def test_no_credential_strings_in_m8_module(path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    body = (project_root / path).read_text()
    for pattern in _FORBIDDEN_PATTERNS:
        assert not re.search(pattern, body), (
            f"Forbidden token {pattern!r} appears in {path} "
            "— M8 code path must NOT touch credentials."
        )
