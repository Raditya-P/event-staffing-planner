"""Sign-in through an OpenID Connect provider (AUTH_MODE=oidc).

Two flows share one identity provider:
- Claude -> /mcp: Claude runs OAuth with the provider itself (it registers through DCR or CIMD)
  and sends an access token. This server only *verifies* that token (a resource server):
  signature against the provider's published keys, issuer, expiry, and audience if configured.
- Browser -> dashboard: the classic authorization-code flow with PKCE; the result is a signed
  session cookie.

Either way the person is identified by (issuer, subject) and mapped to a workspace in accounts.py.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from urllib.parse import urlencode

import httpx
import jwt
from anyio import to_thread
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from .config import settings

log = logging.getLogger(__name__)
ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]


class OidcProvider:
    """Discovery document and signing keys of one issuer, fetched lazily and cached."""

    def __init__(self, issuer: str):
        self.issuer = issuer.rstrip("/")
        self._meta: dict | None = None
        self._jwks: jwt.PyJWKClient | None = None
        self._lock = threading.Lock()

    def metadata(self) -> dict:
        with self._lock:
            if self._meta is None:
                last_error = None
                for path in ("/.well-known/openid-configuration", "/.well-known/oauth-authorization-server"):
                    try:
                        resp = httpx.get(self.issuer + path, timeout=10)
                        if resp.status_code == 200:
                            self._meta = resp.json()
                            break
                        last_error = f"{path}: HTTP {resp.status_code}"
                    except httpx.HTTPError as exc:
                        last_error = f"{path}: {exc}"
                if self._meta is None:
                    raise RuntimeError(f"Could not read the identity provider's discovery document ({last_error}).")
            return self._meta

    def jwks(self) -> jwt.PyJWKClient:
        if self._jwks is None:
            uri = self.metadata()["jwks_uri"]  # takes the lock itself, so call it before taking it here
            with self._lock:
                if self._jwks is None:
                    self._jwks = jwt.PyJWKClient(uri, cache_keys=True, lifespan=3600)
        return self._jwks

    def decode(self, token: str, audience: str | None) -> dict:
        key = self.jwks().get_signing_key_from_jwt(token).key
        return jwt.decode(
            token,
            key,
            algorithms=ALGORITHMS,
            issuer=self.metadata().get("issuer", self.issuer),
            audience=audience or None,
            options={"verify_aud": bool(audience), "require": ["exp", "iss", "sub"]},
            leeway=30,
        )


_provider: OidcProvider | None = None


def provider() -> OidcProvider:
    global _provider
    if _provider is None:
        _provider = OidcProvider(settings.oidc_issuer)
    return _provider


# ---------------------------------------------------------------- MCP: verify Claude's access tokens


class JwtTokenVerifier:
    """The MCP SDK calls this for every request to /mcp that carries a bearer token."""

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await to_thread.run_sync(provider().decode, token, settings.mcp_audience)
        except Exception as exc:  # invalid, expired, wrong issuer/audience, unknown key
            log.info("rejected MCP token: %s", type(exc).__name__)
            return None
        scopes = claims.get("scope") or claims.get("scp") or []
        if isinstance(scopes, str):
            scopes = scopes.split()
        aud = claims.get("aud")
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or claims.get("azp") or ""),
            scopes=list(scopes),
            expires_at=claims.get("exp"),
            resource=aud if isinstance(aud, str) else None,
            subject=claims["sub"],
            claims=claims,
        )


def identity_from_token(token: AccessToken) -> tuple[str, str, str | None, str | None]:
    claims = token.claims or {}
    return claims.get("iss", settings.oidc_issuer), token.subject or "", claims.get("email"), claims.get("name")


# ---------------------------------------------------------------- dashboard: browser sign-in


def _redirect_uri() -> str:
    return f"{settings.base_url}/auth/callback"


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def login(request: Request) -> Response:
    meta = await to_thread.run_sync(provider().metadata)
    verifier, challenge = _pkce()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    request.session["oidc"] = {"state": state, "nonce": nonce, "verifier": verifier, "at": int(time.time())}
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": _redirect_uri(),
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
    }
    if settings.oidc_pkce:
        params.update(code_challenge=challenge, code_challenge_method="S256")
    return RedirectResponse(f"{meta['authorization_endpoint']}?{urlencode(params)}", status_code=302)


async def callback(request: Request) -> Response:
    pending = request.session.pop("oidc", None)
    if not pending or request.query_params.get("state") != pending["state"] or time.time() - pending["at"] > 600:
        return JSONResponse({"error": "Sign-in expired or was tampered with. Please try again."}, status_code=400)
    if "error" in request.query_params:
        return JSONResponse({"error": f"Sign-in failed: {request.query_params.get('error')}"}, status_code=400)
    code = request.query_params.get("code", "")
    meta = await to_thread.run_sync(provider().metadata)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(),
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
                **({"code_verifier": pending["verifier"]} if settings.oidc_pkce else {}),
            },
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200 or "id_token" not in resp.json():
        log.warning("token exchange failed: HTTP %s", resp.status_code)
        return JSONResponse({"error": "Sign-in failed at the token exchange."}, status_code=400)
    try:
        claims = await to_thread.run_sync(provider().decode, resp.json()["id_token"], settings.oidc_client_id)
    except Exception as exc:
        log.warning("id_token rejected: %s", type(exc).__name__)
        return JSONResponse({"error": "Sign-in failed: the identity token was not valid."}, status_code=400)
    # Providers that echo the nonce must echo ours; some (e.g. WorkOS Connect) don't include it at all, and then the
    # state check plus the confidential client secret still bind this response to the request we started.
    if "nonce" in claims and claims["nonce"] != pending["nonce"]:
        return JSONResponse({"error": "Sign-in failed: nonce mismatch."}, status_code=400)
    request.session["user"] = {
        "iss": claims["iss"],
        "sub": claims["sub"],
        "email": claims.get("email"),
        "name": claims.get("name"),
    }
    return RedirectResponse("/", status_code=302)


async def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/", status_code=302)


def session_user(request: Request) -> dict | None:
    if "session" not in request.scope:
        return None
    return request.session.get("user")
