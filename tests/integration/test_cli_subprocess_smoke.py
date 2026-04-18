"""Smoke test M9 via subprocess (opt-in `pytest -m integration`).

Lance `python -m polycopy --dry-run` dans 3 modes (silent, verbose, no-cli)
et vérifie : stdout attendu, fichier log écrit, permissions 0o600, pas de
secret leak. ~10-15 s par test (lent, c'est pourquoi opt-in).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _spawn_bot(
    tmp_path: Path,
    extra_args: list[str],
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env.update(
        {
            "TARGET_WALLETS": "0x1111111111111111111111111111111111111111",
            "DRY_RUN": "true",
            "LOG_FILE": str(tmp_path / "polycopy.log"),
            "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/test.db",
            "DASHBOARD_ENABLED": "false",
            "DISCOVERY_ENABLED": "false",
            "TELEGRAM_BOT_TOKEN": "",
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, "-m", "polycopy", "--dry-run", *extra_args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[2],
    )


@pytest.mark.integration
def test_silent_mode_renders_rich_no_json_stdout(tmp_path: Path) -> None:
    proc = _spawn_bot(tmp_path, [])
    time.sleep(5)
    proc.send_signal(signal.SIGINT)
    try:
        stdout, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, _ = proc.communicate()
    out = stdout.decode("utf-8", errors="replace")
    assert "polycopy" in out
    assert "Watcher" in out
    # Pas de JSON event sur stdout en mode silent.
    assert '"event": "polycopy_starting"' not in out
    # Fichier log écrit avec JSON dedans.
    log_path = tmp_path / "polycopy.log"
    assert log_path.exists()
    log_content = log_path.read_text()
    assert '"event": "polycopy_starting"' in log_content
    # Permissions 0o600.
    mode = oct(log_path.stat().st_mode)[-3:]
    assert mode == "600"


@pytest.mark.integration
def test_verbose_mode_streams_json_stdout(tmp_path: Path) -> None:
    proc = _spawn_bot(tmp_path, ["--verbose"])
    time.sleep(5)
    proc.send_signal(signal.SIGINT)
    try:
        stdout, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, _ = proc.communicate()
    out = stdout.decode("utf-8", errors="replace")
    # JSON présent sur stdout en --verbose.
    assert '"event": "polycopy_starting"' in out
    # Fichier toujours écrit.
    log_path = tmp_path / "polycopy.log"
    assert log_path.exists()
    assert '"event": "polycopy_starting"' in log_path.read_text()


@pytest.mark.integration
def test_no_cli_mode_silent_stdout(tmp_path: Path) -> None:
    proc = _spawn_bot(tmp_path, ["--no-cli"])
    time.sleep(5)
    proc.send_signal(signal.SIGINT)
    try:
        stdout, stderr = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    # Pas d'écran rich, pas de JSON stdout.
    assert b"polycopy" not in stdout or stdout.strip() == b""
    # Fichier écrit.
    log_path = tmp_path / "polycopy.log"
    assert log_path.exists()
    assert '"event": "polycopy_starting"' in log_path.read_text()


@pytest.mark.integration
def test_no_secret_leak_in_log_file(tmp_path: Path) -> None:
    """Aucun secret env ne doit apparaître dans le fichier log."""
    fake_token = "9999999:fakeBotTokenSecretValue"  # noqa: S105
    fake_pk = "0x" + "deadbeef" * 8
    proc = _spawn_bot(
        tmp_path,
        [],
        extra_env={
            "TELEGRAM_BOT_TOKEN": fake_token,
            "POLYMARKET_PRIVATE_KEY": fake_pk,
        },
    )
    time.sleep(5)
    proc.send_signal(signal.SIGINT)
    try:
        proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
    log_path = tmp_path / "polycopy.log"
    assert log_path.exists()
    content = log_path.read_text()
    assert fake_token not in content, "Telegram token leaké dans le fichier log !"
    assert fake_pk not in content, "Private key leakée dans le fichier log !"
