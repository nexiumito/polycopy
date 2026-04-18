"""Tests du ``AlertDigestWindow`` (compteur glissant + reset)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from polycopy.monitoring.alert_digest import AlertDigestWindow
from polycopy.monitoring.dtos import Alert

_BASE_NOW = datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)


def _alert(event: str = "foo") -> Alert:
    return Alert(level="INFO", event=event, body="b")


def test_register_under_threshold_returns_emit_single() -> None:
    win = AlertDigestWindow(window_seconds=3600, threshold=5)
    for i in range(4):
        dec = win.register(_alert(), _BASE_NOW + timedelta(minutes=i))
        assert dec.action == "emit_single"
        assert dec.count == i + 1


def test_register_at_threshold_returns_emit_digest_and_resets() -> None:
    win = AlertDigestWindow(window_seconds=3600, threshold=5)
    for i in range(4):
        win.register(_alert(), _BASE_NOW + timedelta(minutes=i))
    dec5 = win.register(_alert(), _BASE_NOW + timedelta(minutes=5))
    assert dec5.action == "emit_digest"
    assert dec5.count == 5
    # Next alert starts counting from zero again
    dec6 = win.register(_alert(), _BASE_NOW + timedelta(minutes=6))
    assert dec6.action == "emit_single"
    assert dec6.count == 1


def test_out_of_window_events_are_purged() -> None:
    win = AlertDigestWindow(window_seconds=60, threshold=5)
    # 4 alertes à t=0
    for i in range(4):
        win.register(_alert(), _BASE_NOW + timedelta(seconds=i))
    # 70 s plus tard, toutes devraient être purgées → retour à 1
    dec = win.register(_alert(), _BASE_NOW + timedelta(seconds=70))
    assert dec.action == "emit_single"
    assert dec.count == 1


def test_event_types_are_independent() -> None:
    win = AlertDigestWindow(window_seconds=3600, threshold=3)
    win.register(_alert("A"), _BASE_NOW)
    win.register(_alert("A"), _BASE_NOW + timedelta(seconds=1))
    dec_b = win.register(_alert("B"), _BASE_NOW + timedelta(seconds=2))
    assert dec_b.count == 1
    assert dec_b.action == "emit_single"


def test_ten_events_yield_two_digests() -> None:
    win = AlertDigestWindow(window_seconds=3600, threshold=5)
    actions: list[str] = []
    for i in range(10):
        dec = win.register(_alert(), _BASE_NOW + timedelta(minutes=i))
        actions.append(dec.action)
    assert actions == [
        "emit_single",
        "emit_single",
        "emit_single",
        "emit_single",
        "emit_digest",
        "emit_single",
        "emit_single",
        "emit_single",
        "emit_single",
        "emit_digest",
    ]


def test_invalid_params_raise() -> None:
    with pytest.raises(ValueError):
        AlertDigestWindow(window_seconds=0, threshold=5)
    with pytest.raises(ValueError):
        AlertDigestWindow(window_seconds=60, threshold=1)
