"""Tests du ``AlertDispatcher`` (cooldown + envoi Telegram mocké)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from polycopy.config import Settings
from polycopy.monitoring import alert_dispatcher as dispatcher_module
from polycopy.monitoring.alert_dispatcher import AlertDispatcher
from polycopy.monitoring.dtos import Alert


def _settings(cooldown: int = 60) -> Settings:
    return Settings(_env_file=None, alert_cooldown_seconds=cooldown)  # type: ignore[call-arg]


def _fake_telegram(enabled: bool = True, ok: bool = True) -> AsyncMock:
    tg = AsyncMock()
    tg.enabled = enabled
    tg.send = AsyncMock(return_value=ok)
    return tg


@pytest.fixture(autouse=True)
def _fast_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher_module, "_QUEUE_GET_TIMEOUT_SECONDS", 0.02)


async def _stop_after(stop_event: asyncio.Event, delay: float) -> None:
    await asyncio.sleep(delay)
    stop_event.set()


# --- 1 alerte → 1 send ------------------------------------------------------


@pytest.mark.asyncio
async def test_single_alert_sent() -> None:
    q: asyncio.Queue[Alert] = asyncio.Queue()
    q.put_nowait(Alert(level="INFO", event="foo", body="bar"))
    tg = _fake_telegram()
    dispatcher = AlertDispatcher(q, tg, _settings())
    stop = asyncio.Event()
    await asyncio.gather(dispatcher.run(stop), _stop_after(stop, 0.1))
    assert tg.send.call_count == 1


# --- Même cooldown_key dans la fenêtre → 2e throttled -----------------------


@pytest.mark.asyncio
async def test_cooldown_throttles_second_alert() -> None:
    q: asyncio.Queue[Alert] = asyncio.Queue()
    alert = Alert(level="INFO", event="foo", body="bar", cooldown_key="k")
    q.put_nowait(alert)
    q.put_nowait(alert)
    tg = _fake_telegram()
    dispatcher = AlertDispatcher(q, tg, _settings(cooldown=60))
    stop = asyncio.Event()
    await asyncio.gather(dispatcher.run(stop), _stop_after(stop, 0.1))
    assert tg.send.call_count == 1


# --- Cooldown expiré → 2 sends ---------------------------------------------


@pytest.mark.asyncio
async def test_cooldown_expired_allows_resend() -> None:
    q: asyncio.Queue[Alert] = asyncio.Queue()
    alert = Alert(level="INFO", event="foo", body="bar", cooldown_key="k")
    q.put_nowait(alert)
    q.put_nowait(alert)
    tg = _fake_telegram()
    dispatcher = AlertDispatcher(q, tg, _settings(cooldown=5))

    # Simule l'écoulement du temps en bumpant directement l'horloge interne.
    original_now = dispatcher._now  # type: ignore[attr-defined]
    call_count = {"n": 0}

    def _fake_now() -> datetime:
        call_count["n"] += 1
        # 1ère alerte → now=t0, 2e alerte → now=t0+10s (> cooldown=5s)
        base = original_now()
        if call_count["n"] == 1:
            return base
        return base + timedelta(seconds=10)

    dispatcher._now = _fake_now  # type: ignore[assignment, method-assign]
    stop = asyncio.Event()
    await asyncio.gather(dispatcher.run(stop), _stop_after(stop, 0.1))
    assert tg.send.call_count == 2


# --- cooldown_key None → jamais throttle ------------------------------------


@pytest.mark.asyncio
async def test_no_cooldown_key_never_throttled() -> None:
    q: asyncio.Queue[Alert] = asyncio.Queue()
    for _ in range(3):
        q.put_nowait(Alert(level="INFO", event="foo", body="bar", cooldown_key=None))
    tg = _fake_telegram()
    dispatcher = AlertDispatcher(q, tg, _settings())
    stop = asyncio.Event()
    await asyncio.gather(dispatcher.run(stop), _stop_after(stop, 0.1))
    assert tg.send.call_count == 3


# --- Stop event → loop sort proprement --------------------------------------


@pytest.mark.asyncio
async def test_stop_event_clean_exit() -> None:
    q: asyncio.Queue[Alert] = asyncio.Queue()
    tg = _fake_telegram()
    dispatcher = AlertDispatcher(q, tg, _settings())
    stop = asyncio.Event()
    stop.set()  # déjà set au démarrage
    await dispatcher.run(stop)
    assert tg.send.call_count == 0


# --- Guard : datetime never leaks dry_run into runtime ---------------------


@pytest.mark.asyncio
async def test_static_now_returns_aware_utc() -> None:
    now = AlertDispatcher._now()
    assert now.tzinfo is UTC
