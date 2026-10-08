"""Settings, read from environment variables (and a local .env file).

Everything that differs between your laptop and a public deployment lives here,
so the code itself does not change when you move it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in _env(name, default).split(",") if item.strip()]


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _database_url() -> str:
    url = _env("DATABASE_URL")
    if not url:
        # No Neon URL configured yet: fall back to a local file so the app and tests still run.
        return f"sqlite:///{(PROJECT_ROOT / 'local.db').as_posix()}"
    # Neon hands out postgres:// or postgresql:// URLs; SQLAlchemy needs the driver named.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


# ENGINE_PROFILE=light suits small free hosts (a fraction of a CPU): smaller ensemble, shorter search.
_PROFILES = {
    "standard": {"FORECAST_MEMBERS": 10, "FORECAST_BOOST_ITERS": 80, "OPTIMIZER_POP_SIZE": 80,
                 "OPTIMIZER_GENERATIONS": 120, "OPTIMIZER_SAMPLES": 40},
    "light": {"FORECAST_MEMBERS": 6, "FORECAST_BOOST_ITERS": 50, "OPTIMIZER_POP_SIZE": 50,
              "OPTIMIZER_GENERATIONS": 60, "OPTIMIZER_SAMPLES": 25},
}


def _engine(name: str) -> int:
    profile = _PROFILES.get(_env("ENGINE_PROFILE", "standard").lower(), _PROFILES["standard"])
    return _int(name, profile[name])


def _production() -> bool:
    return _env("ENV", "development").lower() == "production"


@dataclass(frozen=True)
class Settings:
    environment: str = field(default_factory=lambda: "production" if _production() else "development")
    database_url: str = field(default_factory=_database_url)
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("PORT", 8000))
    # The address people and Claude use to reach this server, e.g. https://forecast.example.com (no trailing slash).
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL").rstrip("/"))
    # Extra host names allowed to reach /mcp (a tunnel or deployment domain), comma-separated.
    allowed_hosts: list[str] = field(default_factory=lambda: _list("ALLOWED_HOSTS"))

    # Sign-in. "none": one local user (development). "oidc": users sign in through an OpenID Connect provider.
    auth_mode: str = field(default_factory=lambda: _env("AUTH_MODE", "none").lower())
    oidc_issuer: str = field(default_factory=lambda: _env("OIDC_ISSUER").rstrip("/"))
    oidc_client_id: str = field(default_factory=lambda: _env("OIDC_CLIENT_ID"))
    oidc_client_secret: str = field(default_factory=lambda: _env("OIDC_CLIENT_SECRET"))
    # Expected `aud` of access tokens Claude presents to /mcp. Empty = not checked (only safe with a dedicated issuer).
    mcp_audience: str = field(default_factory=lambda: _env("MCP_AUDIENCE"))
    mcp_required_scopes: list[str] = field(default_factory=lambda: _list("MCP_REQUIRED_SCOPES"))
    session_secret: str = field(default_factory=lambda: _env("SESSION_SECRET"))
    # Send PKCE on the dashboard login. Some providers accept it only for public clients; set 0 for those.
    oidc_pkce: bool = field(default_factory=lambda: _env("OIDC_PKCE", "1") == "1")
    # Shown on the privacy page as the person to contact about data.
    contact_email: str = field(default_factory=lambda: _env("CONTACT_EMAIL"))
    # Behind a host's proxy (production), trust X-Forwarded-For so limits apply per real client.
    trust_proxy: bool = field(default_factory=lambda: _env("TRUST_PROXY", "1" if _production() else "0") == "1")

    # Used when AUTH_MODE=none.
    default_actor: str = field(default_factory=lambda: _env("DEFAULT_ACTOR", "planner@local"))
    default_workspace: str = field(default_factory=lambda: _env("DEFAULT_WORKSPACE", "demo"))

    # Limits that keep one user from exhausting a shared server.
    rate_limit_per_minute: int = field(default_factory=lambda: _int("RATE_LIMIT_PER_MINUTE", 120))
    max_what_ifs_per_event: int = field(default_factory=lambda: _int("MAX_WHAT_IFS_PER_EVENT", 30))
    max_pending_per_event: int = field(default_factory=lambda: _int("MAX_PENDING_PER_EVENT", 50))

    # Forecast ensemble size and optimizer budget; tests shrink these for speed.
    forecast_members: int = field(default_factory=lambda: _engine("FORECAST_MEMBERS"))
    forecast_boost_iters: int = field(default_factory=lambda: _engine("FORECAST_BOOST_ITERS"))
    optimizer_pop_size: int = field(default_factory=lambda: _engine("OPTIMIZER_POP_SIZE"))
    optimizer_generations: int = field(default_factory=lambda: _engine("OPTIMIZER_GENERATIONS"))
    optimizer_samples: int = field(default_factory=lambda: _engine("OPTIMIZER_SAMPLES"))
    seed: int = field(default_factory=lambda: _int("SEED", 7))
    # How often an idle worker checks the database for runs queued elsewhere. Long, so the database can sleep.
    worker_idle_poll_seconds: int = field(default_factory=lambda: _int("WORKER_IDLE_POLL_SECONDS", 600))

    # /dev pages (a stand-in chat host for trying the panel). Off by default in production.
    dev_routes: bool = field(default_factory=lambda: _env("DEV_ROUTES", "0" if _production() else "1") == "1")
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "info"))

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def auth_enabled(self) -> bool:
        return self.auth_mode == "oidc"

    @property
    def base_url(self) -> str:
        return self.public_base_url or f"http://{self.host}:{self.port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    def problems(self) -> list[str]:
        """Settings that would make a public deployment unsafe or broken."""
        issues = []
        if self.auth_mode not in ("none", "oidc"):
            issues.append(f"AUTH_MODE must be 'none' or 'oidc', not '{self.auth_mode}'.")
        if self.auth_enabled:
            for name in ("public_base_url", "oidc_issuer", "oidc_client_id", "oidc_client_secret", "session_secret"):
                if not getattr(self, name):
                    issues.append(f"AUTH_MODE=oidc needs {name.upper()}.")
            if self.public_base_url and not self.public_base_url.startswith("https://") and self.is_production:
                issues.append("PUBLIC_BASE_URL must use https in production.")
        if self.is_production:
            if not self.auth_enabled:
                issues.append("Production without sign-in: anyone with the URL can read and change data (AUTH_MODE=none).")
            if self.dev_routes:
                issues.append("DEV_ROUTES=1 in production exposes the /dev pages.")
            if len(self.session_secret) < 32 and self.auth_enabled:
                issues.append("SESSION_SECRET should be at least 32 random characters.")
        return issues


settings = Settings()
