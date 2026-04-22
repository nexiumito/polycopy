"""Tests du ``StartupNotifier`` (build context + bypass si Telegram disabled)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.startup_notifier import StartupNotifier
from polycopy.monitoring.telegram_client import TelegramClient
from polycopy.storage.repositories import TargetTraderRepository

_TOKEN = "123:abc"
_CHAT = "42"


def _settings(**kw: object) -> Settings:
    base: dict[str, Any] = dict(
        telegram_bot_token=_TOKEN,
        telegram_chat_id=_CHAT,
        dashboard_enabled=True,
    )
    base.update(kw)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _disabled_settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest_asyncio.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.mark.asyncio
async def test_startup_skipped_when_telegram_disabled(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    tg = TelegramClient(http_client, _disabled_settings())
    notifier = StartupNotifier(session_factory, tg, AlertRenderer(), _settings())
    stop = asyncio.Event()
    await notifier.send_once(stop)
    # Pas d'envoi, pas d'exception.


@pytest.mark.asyncio
async def test_startup_sent_with_context(
    session_factory: async_sessionmaker[AsyncSession],
    target_trader_repo: TargetTraderRepository,
    http_client: httpx.AsyncClient,
    sample_telegram_response: dict[str, Any],
) -> None:
    await target_trader_repo.upsert("0x" + "a" * 40, label="Smart Money")
    await target_trader_repo.upsert("0x" + "b" * 40, label=None)

    settings = _settings()
    tg = TelegramClient(http_client, settings)
    notifier = StartupNotifier(session_factory, tg, AlertRenderer(), settings)
    stop = asyncio.Event()

    with respx.mock() as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json=sample_telegram_response),
        )
        await notifier.send_once(stop)

    assert route.called
    body = route.calls[0].request.content.decode()
    # Le message contient la marque polycopy + au moins un wallet
    assert "polycopy" in body
    # Le label Smart Money doit apparaître (échappé)
    assert "Smart Money" in body


@pytest.mark.asyncio
async def test_startup_fail_safe_on_400(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
) -> None:
    settings = _settings()
    tg = TelegramClient(http_client, settings)
    notifier = StartupNotifier(session_factory, tg, AlertRenderer(), settings)
    with respx.mock() as mock:
        mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(400, json={"ok": False, "description": "bad"}),
        )
        await notifier.send_once(asyncio.Event())  # pas d'exception propagée


@pytest.mark.asyncio
async def test_startup_noop_if_stop_event_already_set(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tg = AsyncMock()
    tg.enabled = True
    tg.send = AsyncMock(return_value=True)
    notifier = StartupNotifier(session_factory, tg, AlertRenderer(), _settings())
    stop = asyncio.Event()
    stop.set()
    await notifier.send_once(stop)
    tg.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_no_pinned_wallets(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    sample_telegram_response: dict[str, Any],
) -> None:
    settings = _settings()
    tg = TelegramClient(http_client, settings)
    notifier = StartupNotifier(session_factory, tg, AlertRenderer(), settings)
    with respx.mock() as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json=sample_telegram_response),
        )
        await notifier.send_once(asyncio.Event())
    assert route.called
    body = route.calls[0].request.content.decode()
    assert "Aucun wallet pinned" in body
    # Discovery off (default) → suggestion d'activer TARGET_WALLETS ou DISCOVERY_ENABLED
    assert "activer" in body
    assert "DISCOVERY_ENABLED" in body


@pytest.mark.asyncio
async def test_startup_watcher_count_reflects_discovery_actives(
    session_factory: async_sessionmaker[AsyncSession],
    target_trader_repo: TargetTraderRepository,
    http_client: httpx.AsyncClient,
    sample_telegram_response: dict[str, Any],
) -> None:
    """M5_ter fix : watcher_count inclut les actives auto-découverts, pas
    seulement ``len(TARGET_WALLETS)``.

    Scénario reproduit depuis la prod 2026-04-22 : 0 wallet pinned via
    env mais 7 wallets déjà en DB promus par Discovery précédemment.
    Le startup message doit afficher "7 wallets" pas "0 wallets".
    """
    # Seed 7 wallets en DB via insert_shadow + transition_status (comme Discovery).
    for i in range(7):
        wallet = f"0x{str(i).zfill(40)}"
        await target_trader_repo.insert_shadow(wallet)
        await target_trader_repo.transition_status(wallet, new_status="active")

    # TARGET_WALLETS reste vide — simule le cas user avec Discovery seule.
    settings = _settings(discovery_enabled=True)
    tg = TelegramClient(http_client, settings)
    notifier = StartupNotifier(session_factory, tg, AlertRenderer(), settings)
    with respx.mock() as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json=sample_telegram_response),
        )
        await notifier.send_once(asyncio.Event())
    assert route.called
    body = route.calls[0].request.content.decode()
    # Le count reflète les 7 actives en DB, pas 0.
    assert "7 wallets" in body
    assert "0 wallets" not in body


@pytest.mark.asyncio
async def test_startup_no_pinned_but_discovery_enabled_shows_friendly_message(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    sample_telegram_response: dict[str, Any],
) -> None:
    """M5_ter fix : quand ``DISCOVERY_ENABLED=true`` mais 0 pinned, le
    message doit pointer que Discovery va peupler le pool plutôt que de
    suggérer d'activer Discovery (qui l'est déjà)."""
    settings = _settings(discovery_enabled=True)
    tg = TelegramClient(http_client, settings)
    notifier = StartupNotifier(session_factory, tg, AlertRenderer(), settings)
    with respx.mock() as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json=sample_telegram_response),
        )
        await notifier.send_once(asyncio.Event())
    assert route.called
    body = route.calls[0].request.content.decode()
    # Le message adapté à Discovery actif :
    assert "Discovery actif" in body
    assert "pool se remplit automatiquement" in body
    # L'ancienne suggestion ambiguë n'est plus affichée.
    assert "activer `DISCOVERY_ENABLED`" not in body
    assert "activer \\`DISCOVERY\\_ENABLED\\`" not in body


def _telegram_text(body: str) -> str:
    """Extrait le champ ``text`` du payload JSON envoyé à Telegram.

    Le body est du JSON, donc les ``\\`` de l'échappement MarkdownV2 sont
    doublés dans la sérialisation. En désérialisant, on obtient la chaîne
    MarkdownV2 brute ("127\\.0\\.0\\.1:8787"), plus facile à asserter.
    """
    return str(json.loads(body)["text"])


@pytest.mark.asyncio
async def test_startup_dashboard_line_shows_localhost_when_tailscale_off(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    sample_telegram_response: dict[str, Any],
) -> None:
    """``DASHBOARD_BIND_TAILSCALE=false`` → la ligne Dashboard affiche
    ``127.0.0.1:8787`` (fallback classique)."""
    settings = _settings(
        dashboard_enabled=True,
        dashboard_bind_tailscale=False,
        dashboard_host="127.0.0.1",
        dashboard_port=8787,
    )
    tg = TelegramClient(http_client, settings)
    notifier = StartupNotifier(session_factory, tg, AlertRenderer(), settings)
    with respx.mock() as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json=sample_telegram_response),
        )
        await notifier.send_once(asyncio.Event())
    text = _telegram_text(route.calls[0].request.content.decode())
    # Points échappés par telegram_md_escape dans MarkdownV2.
    assert r"127\.0\.0\.1:8787" in text


@pytest.mark.asyncio
async def test_startup_dashboard_line_reflects_tailscale_bind_when_enabled(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    sample_telegram_response: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Régression : ``DASHBOARD_BIND_TAILSCALE=true`` + tailnet résolu →
    la ligne Dashboard affiche l'URL Tailscale (``machine.tailnet:port``) au
    lieu de ``127.0.0.1:8787`` — cohérent avec le bind effectif d'uvicorn
    et le lien cliquable ``[📊 Dashboard]`` en bas du message."""
    # Tailnet résolu via monkeypatch (évite shell out vers ``tailscale``).
    monkeypatch.setattr(
        "polycopy.monitoring.dashboard_url.resolve_tailnet_name",
        lambda _settings: "tail-abc.ts.net",
    )
    settings = _settings(
        dashboard_enabled=True,
        dashboard_bind_tailscale=True,
        dashboard_host="127.0.0.1",
        dashboard_port=8787,
        machine_id="PC-ELIE",
    )
    tg = TelegramClient(http_client, settings)
    notifier = StartupNotifier(session_factory, tg, AlertRenderer(), settings)
    with respx.mock() as mock:
        route = mock.post(f"https://api.telegram.org/bot{_TOKEN}/sendMessage").mock(
            return_value=httpx.Response(200, json=sample_telegram_response),
        )
        await notifier.send_once(asyncio.Event())
    text = _telegram_text(route.calls[0].request.content.decode())
    # URL Tailscale présente (points + tirets échappés par telegram_md_escape).
    assert r"pc\-elie\.tail\-abc\.ts\.net:8787" in text
    # Pas de ``127.0.0.1:8787`` dans la ligne Dashboard : on asserte
    # l'absence sur la forme escaped pour éviter les faux positifs.
    assert r"127\.0\.0\.1:8787" not in text
