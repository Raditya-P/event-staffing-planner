"""One server, two front doors: the dashboard (HTML + JSON API) and the MCP endpoint at /mcp."""

from __future__ import annotations

import json
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import auth, jobs
from . import services as S
from .accounts import actor_for_identity, local_actor
from .config import settings
from .db import init_db, session_scope
from .mcp_server import mcp, panel_html
from .middleware import RateLimitMiddleware, SecurityHeadersMiddleware
from .seed import ensure_demo_data, reset_workspace_demo

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
# Slim Linux images ship no mime.types, so StaticFiles would serve the font as application/octet-stream.
mimetypes.add_type("font/woff2", ".woff2")


def _actor(request: Request) -> S.Actor:
    """Same seam as the MCP side: the signed-in user's workspace, or the local user without sign-in."""
    if not settings.auth_enabled:
        return local_actor("dashboard")
    user = auth.session_user(request)
    if user is None:
        raise S.ServiceError("Please sign in.", status=401)
    return actor_for_identity(user["iss"], user["sub"], user.get("email"), user.get("name"), "dashboard")


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


async def _run(request: Request, fn) -> Response:
    """Resolve the caller, then run a service call in a worker thread inside one transaction.
    Rule violations become HTTP errors with a readable message."""
    def work():
        actor = _actor(request)
        with session_scope() as s:
            return fn(s, actor)

    try:
        return JSONResponse(await run_in_threadpool(work))
    except S.ServiceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


# ---------------------------------------------------------------- read endpoints


async def api_me(request: Request) -> Response:
    signed_in = (not settings.auth_enabled) or auth.session_user(request) is not None
    out = {"auth_mode": settings.auth_mode, "signed_in": signed_in, "mcp_url": settings.mcp_url,
           "contact": settings.contact_email or None,
           "can_reset": settings.auth_enabled or settings.dev_routes}
    if signed_in:
        try:
            actor = await run_in_threadpool(_actor, request)
            out.update(name=actor.name, workspace_id=actor.workspace_id)
        except S.ServiceError:
            out["signed_in"] = False
    return JSONResponse(out)


async def api_events(request: Request) -> Response:
    return await _run(request, lambda s, a: S.list_events(s, a))


async def api_event(request: Request) -> Response:
    return await _run(request, lambda s, a: S.event_detail(s, a, request.path_params["event_id"]))


async def api_revision(request: Request) -> Response:
    return await _run(request, lambda s, a: {"revision": S._event(s, a, request.path_params["event_id"]).revision})


async def api_audit(request: Request) -> Response:
    return await _run(request, lambda s, a: S.audit_log(s, a, request.path_params["event_id"]))


async def api_scenario(request: Request) -> Response:
    return await _run(request, lambda s, a: S.scenario_state(s, a, request.path_params["scenario_id"]))


# ---------------------------------------------------------------- change endpoints


async def api_create_scenario(request: Request) -> Response:
    b = await _body(request)
    return await _run(request, lambda s, a: {
        "id": S.create_what_if(s, a, b.get("event_id", ""), b.get("name", ""), b.get("base_scenario_id")).id})


async def api_add_constraint(request: Request) -> Response:
    b = await _body(request)
    sid = request.path_params["scenario_id"]
    return await _run(request, lambda s, a: {
        "id": S.add_constraint(s, a, sid, b.get("constraint") or {}, b.get("note_text")).id})


async def api_remove_constraint(request: Request) -> Response:
    cid = request.path_params["constraint_id"]
    return await _run(request, lambda s, a: {"id": S.remove_constraint(s, a, cid).id})


async def api_confirm(request: Request) -> Response:
    cid = request.path_params["constraint_id"]

    def go(s, a):
        row, sc = S.confirm_constraint(s, a, cid)
        return {"id": row.id, "scenario_id": sc.id}

    return await _run(request, go)


async def api_reject(request: Request) -> Response:
    b = await _body(request)
    cid = request.path_params["constraint_id"]
    return await _run(request, lambda s, a: {"id": S.reject_constraint(s, a, cid, b.get("reason")).id})


async def api_select(request: Request) -> Response:
    b = await _body(request)
    sid = request.path_params["scenario_id"]
    return await _run(request, lambda s, a: {"id": S.select_plan(s, a, sid, b.get("solution_id", "")).id})


async def api_undo(request: Request) -> Response:
    sid = request.path_params["scenario_id"]
    return await _run(request, lambda s, a: S.undo_last(s, a, sid))


async def api_discard(request: Request) -> Response:
    sid = request.path_params["scenario_id"]
    return await _run(request, lambda s, a: {"id": S.discard_scenario(s, a, sid).id})


async def api_promote(request: Request) -> Response:
    b = await _body(request)
    sid = request.path_params["scenario_id"]
    return await _run(request, lambda s, a: {"id": S.promote_scenario(s, a, sid, b.get("solution_id", "")).id})


async def api_reset_demo(request: Request) -> Response:
    """Throw away everything in the caller's workspace and load a fresh copy of the demo."""
    def work():
        actor = _actor(request)
        if not settings.auth_enabled and not settings.dev_routes:
            raise S.ServiceError("Resetting is switched off on this server.", status=403)
        reset_workspace_demo(actor.workspace_id)
        return {"ok": True}

    try:
        return JSONResponse(await run_in_threadpool(work))
    except S.ServiceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)


# ---------------------------------------------------------------- pages


async def home(request: Request) -> Response:
    """Signed-out visitors get the guided welcome; signed-in planners get the app."""
    if settings.auth_enabled and auth.session_user(request) is None:
        return FileResponse(STATIC / "welcome.html")
    return FileResponse(STATIC / "app.html")


async def welcome(request: Request) -> Response:
    return FileResponse(STATIC / "welcome.html")


def _public_facts() -> dict:
    """Numbers the welcome pages quote: taken from the latest computed forecast, never typed in by hand."""
    from sqlalchemy import select

    from .db import Run

    with session_scope() as s:
        run = s.scalars(
            select(Run).where(Run.status == "done", Run.forecast.isnot(None)).order_by(Run.finished_at.desc()).limit(1)
        ).first()
        forecast = run.forecast if run else None
    if not forecast:
        return {"available": False}
    bt = forecast.get("backtest") or {}
    return {
        "available": True,
        "coverage": bt.get("coverage_p10_p90"),
        "target": bt.get("target_coverage", 0.8),
        "gates": len(forecast.get("series", [])),
        "history_days": max((s_["history"].get("days", 0) for s_ in forecast.get("series", [])), default=0),
        "thin_gates": [s_["gate_name"] for s_ in forecast.get("series", []) if s_["history"].get("thin")],
    }


async def api_facts(request: Request) -> Response:
    return JSONResponse(await run_in_threadpool(_public_facts))


async def privacy(request: Request) -> Response:
    return FileResponse(STATIC / "privacy.html")


async def health(request: Request) -> Response:
    return JSONResponse({"ok": True})


# Development only: a stand-in for Claude's host, to try the chat panel in a normal browser.
async def dev_panel_host(request: Request) -> Response:
    return FileResponse(STATIC / "dev_host.html")


async def dev_panel_html(request: Request) -> Response:
    return HTMLResponse(panel_html())


async def dev_call_tool(request: Request) -> Response:
    b = await _body(request)
    try:
        result = await mcp.call_tool(b.get("name", ""), b.get("arguments") or {})
    except Exception as exc:  # the real protocol would return this as a tool error
        return JSONResponse({"content": [{"type": "text", "text": str(exc)}], "isError": True})
    return JSONResponse(result.model_dump(mode="json", by_alias=True, exclude_none=True))


async def dev_tools(request: Request) -> Response:
    tools = await mcp.list_tools()
    return JSONResponse([t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools])


def routes() -> list:
    r = [
        Route("/", home),
        Route("/welcome", welcome),
        Route("/privacy", privacy),
        Route("/api/facts", api_facts),
        Route("/healthz", health),
        Route("/api/me", api_me),
        Route("/api/events", api_events),
        Route("/api/events/{event_id}", api_event),
        Route("/api/events/{event_id}/revision", api_revision),
        Route("/api/events/{event_id}/audit", api_audit),
        Route("/api/scenarios", api_create_scenario, methods=["POST"]),
        Route("/api/scenarios/{scenario_id}", api_scenario),
        Route("/api/scenarios/{scenario_id}/constraints", api_add_constraint, methods=["POST"]),
        Route("/api/scenarios/{scenario_id}/select", api_select, methods=["POST"]),
        Route("/api/scenarios/{scenario_id}/undo", api_undo, methods=["POST"]),
        Route("/api/scenarios/{scenario_id}/discard", api_discard, methods=["POST"]),
        Route("/api/scenarios/{scenario_id}/promote", api_promote, methods=["POST"]),
        Route("/api/constraints/{constraint_id}", api_remove_constraint, methods=["DELETE"]),
        Route("/api/constraints/{constraint_id}/confirm", api_confirm, methods=["POST"]),
        Route("/api/constraints/{constraint_id}/reject", api_reject, methods=["POST"]),
        Route("/api/workspace/reset-demo", api_reset_demo, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    if settings.auth_enabled:
        r += [
            Route("/login", auth.login),
            Route("/auth/callback", auth.callback),
            Route("/logout", auth.logout, methods=["GET", "POST"]),
        ]
    if settings.dev_routes:
        r += [
            Route("/dev/panel", dev_panel_host),
            Route("/dev/panel.html", dev_panel_html),
            Route("/dev/call-tool", dev_call_tool, methods=["POST"]),
            Route("/dev/tools", dev_tools),
        ]
    return r


def transport_security() -> TransportSecuritySettings:
    """Which Host headers the MCP endpoint accepts (protects a local server from DNS rebinding)."""
    if "*" in settings.allowed_hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    extra = list(settings.allowed_hosts)
    if settings.public_base_url:
        extra.append(settings.public_base_url.split("://", 1)[-1].split("/", 1)[0])
    for host in extra:
        hosts += [host, f"{host}:*"]
        origins += [f"https://{host}"]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def create_app(seed: bool = True) -> Starlette:
    for problem in settings.problems():
        log.warning("configuration: %s", problem)
    if settings.auth_enabled and [p for p in settings.problems() if "needs" in p]:
        raise RuntimeError("Sign-in is switched on but not fully configured: " + "; ".join(settings.problems()))

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,  # no per-client session state: works behind load balancers and serverless hosts
        json_response=True,
        transport_security=transport_security(),
        host=settings.host,
    )
    app.router.routes.extend(routes())
    # Added last = outermost: limits and headers apply to everything, including /mcp.
    if settings.auth_enabled:
        app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie="fm_session",
                           same_site="lax", https_only=settings.base_url.startswith("https://"), max_age=14 * 24 * 3600)
    app.add_middleware(RateLimitMiddleware, per_minute=settings.rate_limit_per_minute)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.base_url.startswith("https://"))
    mcp_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        await run_in_threadpool(init_db)
        if seed and not settings.auth_enabled:
            await run_in_threadpool(ensure_demo_data, settings.default_workspace)
        jobs.resume_unfinished()
        jobs.start_worker()
        jobs.warm_up()
        async with mcp_lifespan(a):
            yield
        jobs.shutdown()

    app.router.lifespan_context = lifespan
    return app
