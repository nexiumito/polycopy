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
    # M10 : le fallback gagne 1 ligne header badge. La forme M4 (emoji + [event]
    # + body) reste présente en dessous.
    assert body.startswith("_\\[")
    assert "🟢 *\\[future\\_event\\]*" in body
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


# --- M17 MD.2 : bypass digest pour CRITICAL (audit C-002 + M-009) ----------


@pytest.mark.asyncio
async def test_critical_alert_bypasses_digest_window() -> None:
    """CRITICAL en rafale → toutes envoyées immédiatement (pas batchées)."""
    q: asyncio.Queue[Alert] = asyncio.Queue()
    # 6 CRITICAL identiques sans cooldown → toutes émises (pas de batch).
    for i in range(6):
        q.put_nowait(
            Alert(
                level="CRITICAL",
                event="kill_switch_triggered",
                body=f"drawdown {i}",
                cooldown_key=None,
            ),
        )
    tg = _fake_telegram()
    dispatcher = AlertDispatcher(
        q,
        tg,
        _settings(telegram_digest_threshold=3, alert_cooldown_seconds=0),
        renderer=AlertRenderer(),
    )
    stop = asyncio.Event()
    await asyncio.gather(dispatcher.run(stop), _stop_after(stop, 0.2))
    # Sans bypass digest, seulement 5 sends (4 immédiats + 1 digest au seuil).
    # Avec bypass MD.2 : 6 sends individuels (jamais de digest pour CRITICAL).
    assert tg.send.await_count == 6
    for call in tg.send.await_args_list:
        body = call.args[0]
        assert "Digest alertes polycopy" not in body


@pytest.mark.asyncio
async def test_critical_alert_respects_cooldown() -> None:
    """CRITICAL bypass digest mais cooldown 60s reste appliqué (anti-flood §11.4)."""
    q: asyncio.Queue[Alert] = asyncio.Queue()
    # 2 CRITICAL avec même cooldown_key → la 2ᵉ doit être throttlée.
    q.put_nowait(
        Alert(
            level="CRITICAL",
            event="kill_switch_triggered",
            body="first",
            cooldown_key="kill_switch",
        ),
    )
    q.put_nowait(
        Alert(
            level="CRITICAL",
            event="kill_switch_triggered",
            body="second",
            cooldown_key="kill_switch",
        ),
    )
    tg = _fake_telegram()
    dispatcher = AlertDispatcher(q, tg, _settings(alert_cooldown_seconds=60))
    stop = asyncio.Event()
    await asyncio.gather(dispatcher.run(stop), _stop_after(stop, 0.1))
    # Cooldown préservé malgré le bypass digest.
    assert tg.send.await_count == 1


@pytest.mark.asyncio
async def test_non_critical_alert_still_digested() -> None:
    """WARNING / INFO : digest logic préservée (non-régression M7)."""
    q: asyncio.Queue[Alert] = asyncio.Queue()
    # Threshold = 3, on en envoie 3 → digest émis.
    for i in range(3):
        q.put_nowait(Alert(level="WARNING", event="drawdown_warning", body=f"w{i}"))
    tg = _fake_telegram()
    dispatcher = AlertDispatcher(
        q,
        tg,
        _settings(telegram_digest_threshold=3, alert_cooldown_seconds=0),
        renderer=AlertRenderer(),
    )
    stop = asyncio.Event()
    await asyncio.gather(dispatcher.run(stop), _stop_after(stop, 0.15))
    # 3ᵉ alerte → digest message.
    assert tg.send.await_count == 3
    last_body = tg.send.await_args_list[-1].args[0]
    assert "Digest alertes polycopy" in last_body


@pytest.mark.asyncio
async def test_critical_alert_telegram_disabled_no_crash() -> None:
    """telegram_client.send retourne False → pas de raise, log + return."""
    q: asyncio.Queue[Alert] = asyncio.Queue()
    q.put_nowait(
        Alert(level="CRITICAL", event="kill_switch_triggered", body="x"),
    )
    tg = AsyncMock()
    tg.enabled = False
    tg.send = AsyncMock(return_value=False)
    dispatcher = AlertDispatcher(q, tg, _settings(), renderer=AlertRenderer())
    stop = asyncio.Event()
    await asyncio.gather(dispatcher.run(stop), _stop_after(stop, 0.1))
    # send appelé une fois (bypass digest), mais retourne False → pas de crash.
    assert tg.send.await_count == 1
