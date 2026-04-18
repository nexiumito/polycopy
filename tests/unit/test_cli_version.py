"""Tests `polycopy.cli.version` — version + git short SHA.

Mock subprocess pour isoler des conditions du runtime CI (pas de git, etc.).
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from polycopy.cli.version import _git_short_sha, get_version


def test_get_version_returns_string() -> None:
    """Le format `<base>+<sha>` ou juste `<base>` est OK."""
    get_version.cache_clear()
    v = get_version()
    assert isinstance(v, str)
    assert v  # non vide


def test_git_short_sha_handles_missing_git() -> None:
    with patch("polycopy.cli.version.subprocess.run", side_effect=FileNotFoundError):
        assert _git_short_sha() is None


def test_git_short_sha_handles_timeout() -> None:
    with patch(
        "polycopy.cli.version.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=2),
    ):
        assert _git_short_sha() is None


def test_git_short_sha_handles_non_zero_exit() -> None:
    fake = subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="not a repo")
    with patch("polycopy.cli.version.subprocess.run", return_value=fake):
        assert _git_short_sha() is None


def test_git_short_sha_returns_short_sha() -> None:
    fake = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="abc1234\n", stderr="")
    with patch("polycopy.cli.version.subprocess.run", return_value=fake):
        assert _git_short_sha() == "abc1234"


def test_git_short_sha_rejects_too_long_output() -> None:
    fake = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="x" * 50, stderr="")
    with patch("polycopy.cli.version.subprocess.run", return_value=fake):
        assert _git_short_sha() is None


def test_get_version_with_git_concatenates() -> None:
    get_version.cache_clear()
    fake = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="deadbee\n", stderr="")
    with patch("polycopy.cli.version.subprocess.run", return_value=fake):
        v = get_version()
        assert v.endswith("+deadbee")
    get_version.cache_clear()
