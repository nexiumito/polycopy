"""Tests ``--force-resume`` + ``_force_resume_sentinel`` (M12_bis Phase D §4.5)."""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from polycopy.cli.runner import _force_resume_sentinel
from polycopy.config import settings as real_settings
from polycopy.remote_control import SentinelFile


def test_force_resume_clears_existing_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(real_settings, "remote_control_sentinel_path", str(tmp_path / "halt.flag"))
    sentinel = SentinelFile(tmp_path / "halt.flag")
    sentinel.touch(reason="manual_stop")
    assert sentinel.exists()
    with structlog.testing.capture_logs() as events:
        _force_resume_sentinel(structlog.get_logger())
    assert not sentinel.exists()
    # Event `sentinel_force_cleared` présent + `previous` reflète la raison.
    cleared_events = [e for e in events if e.get("event") == "sentinel_force_cleared"]
    assert len(cleared_events) == 1
    assert cleared_events[0].get("was_present") is True
    assert cleared_events[0].get("previous") == "manual_stop"


def test_force_resume_is_noop_when_sentinel_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sentinel absent → log `was_present=False` + no raise."""
    monkeypatch.setattr(real_settings, "remote_control_sentinel_path", str(tmp_path / "halt.flag"))
    with structlog.testing.capture_logs() as events:
        _force_resume_sentinel(structlog.get_logger())
    cleared_events = [e for e in events if e.get("event") == "sentinel_force_cleared"]
    assert len(cleared_events) == 1
    assert cleared_events[0].get("was_present") is False
    assert cleared_events[0].get("previous") is None


def test_force_resume_clears_kill_switch_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cas d'usage critique : kill switch a posé le sentinel → recovery manuelle."""
    monkeypatch.setattr(real_settings, "remote_control_sentinel_path", str(tmp_path / "halt.flag"))
    sentinel = SentinelFile(tmp_path / "halt.flag")
    sentinel.touch(reason="kill_switch")
    _force_resume_sentinel(structlog.get_logger())
    assert not sentinel.exists()
