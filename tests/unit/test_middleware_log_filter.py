"""Tests M10 §4.1 + §8.3 — processor structlog ``filter_noisy_endpoints``.

Couvre :
- GET 200 sur path whitelist → DropEvent (aucune ligne émise par structlog).
- GET 500 sur path whitelist → pass (errors always pass).
- GET 200 sur path non-whitelisté → pass.
- Override env ``DASHBOARD_LOG_SKIP_PATHS`` additif aux defaults.
"""

from __future__ import annotations

import pytest
import structlog

from polycopy.cli.logging_config import make_filter_noisy_endpoints


def test_middleware_drops_noisy_endpoint_success() -> None:
    """GET 200 sur ``/partials/kpis`` → DropEvent."""
    processor = make_filter_noisy_endpoints()
    with pytest.raises(structlog.DropEvent):
        processor(
            None,
            "info",
            {
                "event": "dashboard_request",
                "path": "/partials/kpis",
                "status": 200,
                "method": "GET",
            },
        )


def test_middleware_keeps_noisy_endpoint_error() -> None:
    """GET 500 sur ``/partials/kpis`` → ligne émise (observabilité errors)."""
    processor = make_filter_noisy_endpoints()
    event_dict = {
        "event": "dashboard_request",
        "path": "/partials/kpis",
        "status": 500,
    }
    out = processor(None, "error", event_dict)
    assert out is event_dict


def test_middleware_keeps_non_noisy_endpoint_success() -> None:
    """GET 200 sur ``/home`` → pass (pas dans la whitelist)."""
    processor = make_filter_noisy_endpoints()
    event_dict = {
        "event": "dashboard_request",
        "path": "/home",
        "status": 200,
    }
    out = processor(None, "info", event_dict)
    assert out is event_dict


def test_middleware_keeps_other_events_untouched() -> None:
    """Event ≠ dashboard_request → pass, quel que soit status/path."""
    processor = make_filter_noisy_endpoints()
    event_dict = {"event": "order_simulated", "path": "/partials/kpis"}
    out = processor(None, "info", event_dict)
    assert out is event_dict


def test_skip_paths_env_override_adds_extra() -> None:
    """``DASHBOARD_LOG_SKIP_PATHS`` additif aux defaults hardcodés."""
    processor = make_filter_noisy_endpoints(["^/custom/debug$"])
    with pytest.raises(structlog.DropEvent):
        processor(
            None,
            "info",
            {"event": "dashboard_request", "path": "/custom/debug", "status": 200},
        )
    # Les defaults restent actifs.
    with pytest.raises(structlog.DropEvent):
        processor(
            None,
            "info",
            {"event": "dashboard_request", "path": "/api/health-external", "status": 200},
        )
