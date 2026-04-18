"""Récupère la version du bot — pyproject.toml + git SHA short si dispo.

Cache module-level (évite de relire le pyproject à chaque appel). Pas de
dépendance `tomllib` Python 3.11+ requise pour la part git ; on garde
zéro dep externe.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version


@lru_cache(maxsize=1)
def get_version() -> str:
    """Retourne la version polycopy (`pyproject.toml`) + suffix git court si dispo.

    Format : `0.1.0+abc1234` si dans un repo git, sinon `0.1.0`.
    Ne fait JAMAIS d'I/O réseau.
    """
    try:
        base = version("polycopy")
    except PackageNotFoundError:
        base = "0.0.0-unknown"
    sha = _git_short_sha()
    if sha:
        return f"{base}+{sha}"
    return base


def _git_short_sha() -> str | None:
    """Retourne le SHA court HEAD ou None si pas de git / pas de repo."""
    cmd = ["git", "rev-parse", "--short", "HEAD"]  # noqa: S607 — `git` PATH OK
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    if not sha or len(sha) > 16:
        return None
    return sha
