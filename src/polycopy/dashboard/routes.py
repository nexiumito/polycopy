"""Routes FastAPI du dashboard : pages + partials HTMX + JSON PnL + healthz.

Toutes les routes sont ``GET`` (cf. spec M4.5 §5.2 et M6 §0.6, garanti par
``test_dashboard_security`` + ``test_dashboard_security_m6``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, cast

import httpx
import structlog
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.dashboard import queries
from polycopy.dashboard.health_check import ExternalHealthChecker
from polycopy.dashboard.jinja_filters import all_filters
from polycopy.dashboard.log_reader import (
    _DEFAULT_EXCLUDED_EVENTS,
    filter_entries,
    read_log_tail,
)
from polycopy.dashboard.middleware import StructlogAccessMiddleware

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_MAX_EVENT_FILTERS = 20

log = structlog.get_logger(__name__)

_DASHBOARD_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _DASHBOARD_DIR / "templates"
_STATIC_DIR = _DASHBOARD_DIR / "static"


def _make_templates() -> Jinja2Templates:
    """Construit ``Jinja2Templates`` avec les filtres M6 enregistrés."""
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters.update(all_filters())
    return templates


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Extrait le ``session_factory`` attaché à ``app.state`` (§5.1)."""
    factory = request.app.state.session_factory
    assert isinstance(factory, async_sessionmaker)  # noqa: S101 — invariant app state
    return factory


def get_settings(request: Request) -> Settings:
    """Retourne les ``Settings`` attachés à ``app.state``."""
    return request.app.state.settings  # type: ignore[no-any-return]


def _get_health_checker(request: Request) -> ExternalHealthChecker:
    """Retourne le singleton ``ExternalHealthChecker`` attaché à ``app.state``."""
    return request.app.state.health_checker  # type: ignore[no-any-return]


SFDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
STDep = Annotated[Settings, Depends(get_settings)]


def _render(
    request: Request,
    template_name: str,
    context: dict[str, Any],
) -> HTMLResponse:
    """Wrapper ``TemplateResponse`` typé, injecte les variables UI communes M6.

    - ``settings_dry_run`` : badge DRY-RUN dans la sidebar (M4.5).
    - ``dashboard_theme`` / ``poll_interval`` : tokens cosmétiques (M6).
    """
    templates: Jinja2Templates = request.app.state.templates
    settings: Settings = request.app.state.settings
    base_context: dict[str, Any] = {
        "settings_dry_run": settings.execution_mode != "live",
        "settings_execution_mode": settings.execution_mode,
        "dashboard_theme": settings.dashboard_theme,
        "poll_interval": settings.dashboard_poll_interval_seconds,
    }
    base_context.update(context)
    return templates.TemplateResponse(request, template_name, base_context)


def build_pages_router() -> APIRouter:
    """Router des pages full-HTML (layout ``base.html``)."""
    router = APIRouter()

    @router.get("/", response_class=RedirectResponse)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/home", status_code=307)

    @router.get("/home", response_class=HTMLResponse)
    async def home(request: Request, sf: SFDep, settings: STDep) -> HTMLResponse:
        cards = await queries.get_home_kpi_cards(sf)
        alltime = await queries.get_home_alltime_stats(sf)
        discovery = await queries.get_discovery_status(sf, enabled=settings.discovery_enabled)
        recent_trades = await queries.list_detected_trades(sf, limit=8)
        return _render(
            request,
            "home.html",
            {
                "cards": cards,
                "alltime": alltime,
                "discovery": discovery,
                "recent_trades": recent_trades,
            },
        )

    @router.get("/detections", response_class=HTMLResponse)
    async def detections(request: Request, wallet: str | None = None) -> HTMLResponse:
        return _render(request, "detections.html", {"wallet": wallet or ""})

    @router.get("/strategy", response_class=HTMLResponse)
    async def strategy(request: Request, decision: str | None = None) -> HTMLResponse:
        return _render(request, "strategy.html", {"decision": decision or ""})

    @router.get("/orders", response_class=HTMLResponse)
    async def orders(request: Request, status: str | None = None) -> HTMLResponse:
        return _render(request, "orders.html", {"status": status or ""})

    @router.get("/positions", response_class=HTMLResponse)
    async def positions(request: Request, state: str | None = None) -> HTMLResponse:
        return _render(request, "positions.html", {"state": state or ""})

    @router.get("/pnl", response_class=HTMLResponse)
    async def pnl(
        request: Request,
        sf: SFDep,
        since: str = "24h",
        mode: str = "real",
    ) -> HTMLResponse:
        milestones = await queries.get_pnl_milestones(sf, since=queries.parse_since(since))
        # M8 : ``mode`` ∈ {real, dry_run, both}. Validation côté queries.
        effective_mode = mode if mode in {"real", "dry_run", "both"} else "real"
        return _render(
            request,
            "pnl.html",
            {
                "since": since,
                "milestones": milestones,
                "mode": effective_mode,
            },
        )

    @router.get("/traders", response_class=HTMLResponse)
    async def traders(request: Request, status: str | None = None) -> HTMLResponse:
        return _render(request, "traders.html", {"status_filter": status or ""})

    @router.get("/activity", response_class=HTMLResponse)
    async def activity(request: Request) -> HTMLResponse:
        """Historique des positions fermées (commit 6)."""
        return _render(request, "activity.html", {})

    @router.get("/traders/scoring", response_class=HTMLResponse)
    async def traders_scoring_page(
        request: Request,
        sf: SFDep,
        settings: STDep,
    ) -> HTMLResponse:
        """Onglet M12 — comparaison v1|v2|delta_rank + cutover status.

        Read-only strict (pas d'endpoint write). Le bouton "Validate v2 & flip"
        du template n'exécute pas le flip (respect invariant dashboard M4.5
        read-only) — il affiche la commande ``.env`` à appliquer manuellement.
        """
        rows = await queries.list_scoring_comparison(sf, limit=200)
        aggregates = await queries.scoring_comparison_aggregates(
            sf,
            shadow_days=settings.scoring_v2_shadow_days,
            cutover_ready=settings.scoring_v2_cutover_ready,
        )
        return _render(
            request,
            "traders_scoring.html",
            {
                "rows": rows,
                "aggregates": aggregates,
                "pilot_version": settings.scoring_version,
                "shadow_days_config": settings.scoring_v2_shadow_days,
            },
        )

    @router.get("/backtest", response_class=HTMLResponse)
    async def backtest(request: Request) -> HTMLResponse:
        return _render(
            request,
            "backtest.html",
            {"report_exists": queries.backtest_report_exists()},
        )

    @router.get("/latency", response_class=HTMLResponse)
    async def latency_page(
        request: Request,
        sf: SFDep,
        since: str = "24h",
    ) -> HTMLResponse:
        """Onglet M11 — histogramme latence par stage p50/p95/p99.

        ``since`` ∈ ``{1h, 24h, 7d, 30d}``. Fallback défensif sur 24h.
        """
        percentiles = await queries.compute_latency_percentiles(
            sf,
            since=queries.parse_since(since),
        )
        return _render(
            request,
            "latency.html",
            {
                "since": since,
                "percentiles": percentiles,
            },
        )

    @router.get("/logs", response_class=HTMLResponse)
    async def logs_page(
        request: Request,
        settings: STDep,
        levels: Annotated[list[str] | None, Query()] = None,
        events: Annotated[list[str] | None, Query()] = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
    ) -> HTMLResponse:
        """Onglet logs M9 — lecture fichier `LOG_FILE` + filtres serveur."""
        if not settings.dashboard_logs_enabled:
            return _render(
                request,
                "logs.html",
                {
                    "logs_enabled": False,
                    "disabled_reason": "DASHBOARD_LOGS_ENABLED=false",
                    "entries": [],
                    "filter_levels": [],
                    "filter_events": [],
                    "filter_q": "",
                },
            )
        validated_levels = _validate_levels(levels)
        validated_events = _validate_events(events)
        # M10 : ``dashboard_request`` exclu par défaut ; opt-in via query.
        exclude = None if "dashboard_request" in validated_events else _DEFAULT_EXCLUDED_EVENTS
        all_entries = read_log_tail(settings.log_file, settings.dashboard_logs_tail_lines)
        filtered = filter_entries(
            all_entries,
            levels=set(validated_levels) if validated_levels else None,
            event_types=set(validated_events) if validated_events else None,
            q=q,
            exclude_events=exclude,
        )
        # UI affiche le plus récent en haut (convention logs standard).
        # Le reader reste chronologique — l'inversion vit à l'affichage.
        filtered_reversed = list(reversed(filtered))
        return _render(
            request,
            "logs.html",
            {
                "logs_enabled": True,
                "disabled_reason": "",
                "entries": filtered_reversed,
                "filter_levels": validated_levels,
                "filter_events": validated_events,
                "filter_q": q or "",
            },
        )

    @router.get("/logs/download")
    async def logs_download(settings: STDep) -> FileResponse:
        """Téléchargement du fichier `LOG_FILE` complet (filename hardcodé)."""
        if not settings.dashboard_logs_enabled:
            raise HTTPException(status_code=403, detail="Logs download disabled")
        log_file = settings.log_file
        if not log_file.exists():
            raise HTTPException(status_code=404, detail="Log file not found")
        return FileResponse(
            path=str(log_file),
            media_type="text/plain",
            filename="polycopy.log",
        )

    return router


def _validate_levels(levels: list[str] | None) -> list[str]:
    """Valide la liste de levels — rejette tout invalide via 400."""
    if not levels:
        return []
    cleaned: list[str] = []
    for lvl in levels:
        upper = lvl.upper().strip()
        if upper not in _VALID_LOG_LEVELS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid level '{lvl}' (allowed: {sorted(_VALID_LOG_LEVELS)})",
            )
        cleaned.append(upper)
    return cleaned


def _validate_events(events: list[str] | None) -> list[str]:
    """Valide la liste d'event_types : cap 20, accepte CSV via virgule."""
    if not events:
        return []
    flat: list[str] = []
    for raw in events:
        for piece in raw.split(","):
            piece = piece.strip()  # noqa: PLW2901
            if piece:
                flat.append(piece)
    if len(flat) > _MAX_EVENT_FILTERS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many event filters (max {_MAX_EVENT_FILTERS})",
        )
    return flat


def build_partials_router() -> APIRouter:
    """Router des fragments HTMX (``/partials/*``) + endpoint JSON Chart.js."""
    router = APIRouter(prefix="/partials")

    @router.get("/kpis", response_class=HTMLResponse)
    async def kpis(request: Request, sf: SFDep) -> HTMLResponse:
        cards = await queries.get_home_kpi_cards(sf)
        return _render(request, "partials/kpis.html", {"cards": cards})

    @router.get("/discovery-summary", response_class=HTMLResponse)
    async def discovery_summary(request: Request, sf: SFDep, settings: STDep) -> HTMLResponse:
        discovery = await queries.get_discovery_status(sf, enabled=settings.discovery_enabled)
        return _render(
            request,
            "partials/discovery_summary.html",
            {"discovery": discovery},
        )

    @router.get("/detections-rows", response_class=HTMLResponse)
    async def detections_rows(
        request: Request,
        sf: SFDep,
        wallet: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> HTMLResponse:
        rows = await queries.list_detected_trades(
            sf,
            wallet=wallet,
            limit=limit,
            offset=offset,
        )
        return _render(
            request,
            "partials/detections_rows.html",
            {"rows": rows, "limit": limit, "offset": offset, "wallet": wallet or ""},
        )

    @router.get("/strategy-rows", response_class=HTMLResponse)
    async def strategy_rows(
        request: Request,
        sf: SFDep,
        decision: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> HTMLResponse:
        decision_typed: Literal["APPROVED", "REJECTED"] | None = (
            cast(Literal["APPROVED", "REJECTED"], decision)
            if decision in {"APPROVED", "REJECTED"}
            else None
        )
        rows = await queries.list_strategy_decisions(
            sf,
            decision=decision_typed,
            limit=limit,
            offset=offset,
        )
        return _render(
            request,
            "partials/strategy_rows.html",
            {
                "rows": rows,
                "limit": limit,
                "offset": offset,
                "decision": decision or "",
            },
        )

    @router.get("/orders-rows", response_class=HTMLResponse)
    async def orders_rows(
        request: Request,
        sf: SFDep,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> HTMLResponse:
        rows = await queries.list_orders(
            sf,
            status=status,
            limit=limit,
            offset=offset,
        )
        return _render(
            request,
            "partials/orders_rows.html",
            {"rows": rows, "limit": limit, "offset": offset, "status": status or ""},
        )

    @router.get("/positions-rows", response_class=HTMLResponse)
    async def positions_rows(
        request: Request,
        sf: SFDep,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> HTMLResponse:
        state_typed: Literal["open", "closed"] | None = (
            cast(Literal["open", "closed"], state) if state in {"open", "closed"} else None
        )
        rows = await queries.list_positions(
            sf,
            state=state_typed,
            limit=limit,
            offset=offset,
        )
        return _render(
            request,
            "partials/positions_rows.html",
            {
                "rows": rows,
                "limit": limit,
                "offset": offset,
                "state": state or "",
            },
        )

    @router.get("/pnl-data.json", response_class=JSONResponse)
    async def pnl_data(
        sf: SFDep,
        since: str = "24h",
        include_dry_run: bool = False,
        mode: str | None = None,
    ) -> JSONResponse:
        series = await queries.fetch_pnl_series(
            sf,
            since=queries.parse_since(since),
            include_dry_run=include_dry_run,
            mode=mode,
        )
        payload: dict[str, Any] = {
            "timestamps": [ts.isoformat() for ts in series.timestamps],
            "total_usdc": series.total_usdc,
            "drawdown_pct": series.drawdown_pct,
        }
        return JSONResponse(payload)

    @router.get("/logs-tail", response_class=HTMLResponse)
    async def logs_tail_partial(
        request: Request,
        settings: STDep,
        levels: Annotated[list[str] | None, Query()] = None,
        events: Annotated[list[str] | None, Query()] = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
    ) -> HTMLResponse:
        """Fragment HTMX rafraîchi par live tail (polling 2 s)."""
        if not settings.dashboard_logs_enabled:
            return _render(
                request,
                "partials/logs_tail.html",
                {"entries": []},
            )
        validated_levels = _validate_levels(levels)
        validated_events = _validate_events(events)
        # M10 : même logique d'exclusion default que la page ``/logs``.
        exclude = None if "dashboard_request" in validated_events else _DEFAULT_EXCLUDED_EVENTS
        all_entries = read_log_tail(settings.log_file, settings.dashboard_logs_tail_lines)
        filtered = filter_entries(
            all_entries,
            levels=set(validated_levels) if validated_levels else None,
            event_types=set(validated_events) if validated_events else None,
            q=q,
            exclude_events=exclude,
        )
        # Ordre d'affichage UI : récent → ancien (cohérent avec /logs).
        filtered_reversed = list(reversed(filtered))
        return _render(
            request,
            "partials/logs_tail.html",
            {"entries": filtered_reversed},
        )

    @router.get("/activity-rows", response_class=HTMLResponse)
    async def activity_rows(
        request: Request,
        sf: SFDep,
        limit: int = 50,
        offset: int = 0,
    ) -> HTMLResponse:
        rows = await queries.list_activity_closed_positions(sf, limit=limit, offset=offset)
        return _render(
            request,
            "partials/activity_rows.html",
            {"rows": rows, "limit": limit, "offset": offset},
        )

    @router.get("/traders-rows", response_class=HTMLResponse)
    async def traders_rows(
        request: Request,
        sf: SFDep,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> HTMLResponse:
        rows = await queries.list_traders(
            sf,
            status=status,
            limit=limit,
            offset=offset,
        )
        counts = await queries.count_traders_by_status(sf)
        return _render(
            request,
            "partials/traders_rows.html",
            {
                "rows": rows,
                "counts": counts,
                "status": status or "",
                "limit": limit,
                "offset": offset,
            },
        )

    return router


def build_api_router() -> APIRouter:
    """Router des endpoints API M6 (health-external, version)."""
    router = APIRouter(prefix="/api")

    @router.get("/health-external", response_class=HTMLResponse)
    async def health_external(request: Request) -> HTMLResponse:
        checker = _get_health_checker(request)
        snapshot = await checker.check()
        version = await queries.get_app_version()
        return _render(
            request,
            "partials/external_health.html",
            {"snapshot": snapshot, "version": version},
        )

    @router.get("/version", response_class=JSONResponse)
    async def version_json() -> JSONResponse:
        version = await queries.get_app_version()
        return JSONResponse({"version": version})

    return router


def build_app(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> FastAPI:
    """Construit l'app FastAPI (pas de Swagger, middleware structlog, static mount)."""
    app = FastAPI(
        title="polycopy dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.session_factory = session_factory
    app.state.settings = settings
    app.state.templates = _make_templates()
    # 1 ``httpx.AsyncClient`` partagé pour le health checker (M6 §4 / §14.4 #8).
    # Pas de ``aclose()`` explicite : l'app vit pour la durée du process uvicorn.
    http_client = httpx.AsyncClient()
    app.state.http_client = http_client
    app.state.health_checker = ExternalHealthChecker(http_client)

    app.add_middleware(StructlogAccessMiddleware)

    app.include_router(build_pages_router())
    app.include_router(build_partials_router())
    app.include_router(build_api_router())

    @app.get("/healthz", response_class=JSONResponse)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if _STATIC_DIR.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=str(_STATIC_DIR)),
            name="static",
        )

    log.info(
        "dashboard_routes_registered",
        routes_count=len(app.routes),
    )
    return app
