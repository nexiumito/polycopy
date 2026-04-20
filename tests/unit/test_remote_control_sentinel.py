"""Tests ``SentinelFile`` (M12_bis §5.2 Phase C)."""

from __future__ import annotations

import stat
from pathlib import Path

from polycopy.remote_control import SentinelFile


def test_exists_false_when_file_absent(tmp_path: Path) -> None:
    s = SentinelFile(tmp_path / "halt.flag")
    assert s.exists() is False


def test_touch_creates_file_with_reason(tmp_path: Path) -> None:
    s = SentinelFile(tmp_path / "halt.flag")
    s.touch(reason="kill_switch")
    assert s.exists() is True
    assert s.reason() == "kill_switch"


def test_touch_creates_parent_dir_with_0o700(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "polycopy"
    s = SentinelFile(nested / "halt.flag")
    s.touch(reason="test")
    assert nested.is_dir()
    # Mask à 0o777 pour isoler les bits permission.
    parent_mode = stat.S_IMODE(nested.stat().st_mode)
    assert parent_mode == 0o700


def test_touch_sets_file_0o600(tmp_path: Path) -> None:
    s = SentinelFile(tmp_path / "halt.flag")
    s.touch(reason="test")
    file_mode = stat.S_IMODE((tmp_path / "halt.flag").stat().st_mode)
    assert file_mode == 0o600


def test_touch_is_idempotent_overwrites_reason(tmp_path: Path) -> None:
    s = SentinelFile(tmp_path / "halt.flag")
    s.touch(reason="first")
    s.touch(reason="second")
    assert s.reason() == "second"


def test_clear_removes_file(tmp_path: Path) -> None:
    s = SentinelFile(tmp_path / "halt.flag")
    s.touch(reason="x")
    s.clear()
    assert s.exists() is False


def test_clear_is_noop_when_absent(tmp_path: Path) -> None:
    s = SentinelFile(tmp_path / "halt.flag")
    # Ne doit PAS raise.
    s.clear()
    assert s.exists() is False


def test_reason_none_when_file_absent(tmp_path: Path) -> None:
    s = SentinelFile(tmp_path / "halt.flag")
    assert s.reason() is None


def test_expanduser_resolution(tmp_path: Path, monkeypatch: Path) -> None:
    """`~` dans le chemin doit être expanded au constructeur."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    import os

    os.environ["HOME"] = str(fake_home)
    s = SentinelFile("~/test/halt.flag")
    assert str(s.path).startswith(str(fake_home))


def test_reason_strips_trailing_newline(tmp_path: Path) -> None:
    """`touch` écrit avec \\n final, `reason` doit le strip."""
    s = SentinelFile(tmp_path / "halt.flag")
    s.touch(reason="kill_switch")
    raw = (tmp_path / "halt.flag").read_text()
    assert raw.endswith("\n")
    assert s.reason() == "kill_switch"
