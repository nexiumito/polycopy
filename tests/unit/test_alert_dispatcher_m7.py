"""Tests M7 du ``AlertDispatcher`` (rendu + digest + compteurs)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from polycopy.config import Settings
from polycopy.monitoring import alert_dispatcher as dispatcher_module
from polycopy.monitoring.alert_dispatcher import AlertDispatcher
from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.dtos import Alert


def _settings(**overrides: object) -> Settings:
    base = dict(
        alert_cooldown_seconds=60,
        telegram_digest_threshold=5,
        telegram_digest_window_minutes=60,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _fake_telegram() -> AsyncMock:
    tg = AsyncMock()
    tg.enabled = True
    tg.send = AsyncMock(return_value=True)
    return tg


@pytest.fixture(autouse=True)
def _fast_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher_module, "_QUEUE_GET_TIMEOUT_SECONDS", 0.02)


async def _stop_after(stop_event: asyncio.Event, delay: float) -> None:
    await asyncio.sleep(delay)
    stop_event.set()


# --- Rendering via AlertRenderer (fallback + template dédié) ---------------


@pytest.mark.asyncio
async def test_known_event_renders_via_template() -> None:
    q: asyncio.Queue[Alert] = asyncio.Queue()
    q.put_nowait(
        Alert(level="CRITICAL", event="kill_switch_triggered", body="drawdown 30."),
    )
    tg = _fake_telegram()
    dispatcher = AlertDispatcher(q, tg, _settings(), renderer=AlertRenderer())
    stop = asyncio.Event()
    await asyncio.gather(dispatcher.run(stop), _stop_after(stop, 0.1))
    body = tg.send.await_args.args[0]
    assert "kill\\_switch\\_triggered" in body
    assert "Action requise" in body


@pytest.mark.asyncio
async def test_unknown_event_uses_fallback_preserves_m4_shape() -> None:
    q: asyncio.Queue[Alert] = asyncio.Queue()
    q.put_nowait(Alert(level="INFO", event="future_event", body="x"))
    tg = _fake_telegram()
    dispatcher = AlertDispatcher(q, tg, _settings(), renderer=AlertRenderer())
    stop = asyncio.Event()
    await asyncio.gather(dispatcher.run(stop), _stop_after(stop, 0.1))
    body = tg.send.await_args.args[0]
    # Forme M4 préservée : emoji + [event] + body
    assert body.startswith("🟢")
    assert "future\\_event" in body


# --- Digest trigger ---------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_triggered_at_threshold() -> None:
    q: asyncio.Queue[Alert] = asyncio.Queue()
    # Pas de cooldown_key → tous passent le cooldown.
    for i in range(5):
        q.put_nowait(Alert(level="INFO", event="order_filled_large", body=f"fill {i}"))
    tg = _fake_telegram()
    dispatcher = AlertDispatcher(q, tg, _settings(), renderer=AlertRenderer())
    stop = asyncio.Event()
    await asyncio.gather(dispatcher.run(stop), _stop_after(stop, 0.2))
    assert tg.send.await_count == 5
    # Le 5ᵉ message est un digest.
    last = tg.send.await_args.args[0]
    assert "Digest alertes polycopy" in last
    assert "5 alertes" in last


# --- Compteurs & flag critical ---------------------------------------------


@pytest.mark.asyncio
async def test_counts_since_boot_and_critical_flag() -> None:
    q: asyncio.Queue[Alert] = asyncio.Queue()
    q.put_nowait(Alert(level="INFO", event="order_filled_large", body="a"))
    q.put_nowait(Alert(level="CRITICAL", event="kill_switch_triggered", body="b"))
    tg = _fake_telegram()
    dispatcher = AlertDispatcher(q, tg, _settings(), renderer=AlertRenderer())
    stop = asyncio.Event()
    await asyncio.gather(dispatcher.run(stop), _stop_after(stop, 0.1))
    counts = dispatcher.counts_since_boot
    assert counts["order_filled_large"] == 1
    assert counts["kill_switch_triggered"] == 1
    assert dispatcher.has_recent_critical(timedelta(minutes=5)) is True


# --- Non-régression M4 : cooldown toujours actif ---------------------------


@pytest.mark.asyncio
async def test_m4_cooldown_non_regression() -> None:
    q: asyncio.Queue[Alert] = asyncio.Queue()
    alert = Alert(level="WARNING", event="foo", body="bar", cooldown_key="k")
    q.put_nowait(alert)
    q.put_nowait(alert)
    tg = _fake_telegram()
    dispatcher = AlertDispatcher(q, tg, _settings(alert_cooldown_seconds=60))
    stop = asyncio.Event()
    await asyncio.gather(dispatcher.run(stop), _stop_after(stop, 0.1))
    assert tg.send.await_count == 1
