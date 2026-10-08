"""Start the server: `uv run forecast-mcp` (dashboard on /, MCP endpoint on /mcp)."""

from __future__ import annotations

import logging

import uvicorn

from .config import settings


def main() -> None:
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .web import create_app

    db_kind = "Postgres (DATABASE_URL)" if settings.is_postgres else "local SQLite file (no DATABASE_URL set)"
    print(f"Database: {db_kind}")
    print(f"Sign-in:  {'OpenID Connect via ' + settings.oidc_issuer if settings.auth_enabled else 'off (single local user)'}")
    print(f"Dashboard:    {settings.base_url}/")
    print(f"MCP endpoint: {settings.mcp_url}")
    for problem in settings.problems():
        print(f"WARNING: {problem}")
    uvicorn.run(
        create_app(),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=settings.trust_proxy,
        forwarded_allow_ips="*" if settings.trust_proxy else "127.0.0.1",
    )


if __name__ == "__main__":
    main()
