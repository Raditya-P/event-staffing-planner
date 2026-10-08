"""Deployment plumbing: migrations match the models, limits, headers, settings checks."""

import dataclasses

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from forecast_mcp import config, db
from forecast_mcp.middleware import RateLimitMiddleware, SecurityHeadersMiddleware


def test_migrations_match_the_models():
    with db.engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() is not None
        diff = compare_metadata(MigrationContext.configure(conn), db.Base.metadata)
    assert diff == [], f"db.py changed without a migration: {diff}"


def _app():
    async def page(request):
        return HTMLResponse("<p>hi</p>")

    async def api(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/", page), Route("/api/x", api), Route("/api/events/e/revision", api)])
    app.add_middleware(RateLimitMiddleware, per_minute=3)
    app.add_middleware(SecurityHeadersMiddleware, hsts=True)
    return app


def test_rate_limit_and_headers():
    client = TestClient(_app())
    codes = [client.get("/api/x").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429
    assert all(client.get("/api/events/e/revision").status_code == 200 for _ in range(5))  # polling is exempt
    page = client.get("/")
    assert page.status_code == 200  # pages are not rate limited
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert "max-age" in page.headers["strict-transport-security"]
    assert client.get("/api/x").headers.get("x-content-type-options") == "nosniff"


def test_production_settings_are_checked():
    prod = dataclasses.replace(config.settings, environment="production", auth_mode="none", dev_routes=True)
    problems = " ".join(prod.problems())
    assert "without sign-in" in problems and "DEV_ROUTES" in problems
    half = dataclasses.replace(config.settings, auth_mode="oidc", oidc_issuer="", public_base_url="")
    assert any("OIDC_ISSUER" in p for p in half.problems())
