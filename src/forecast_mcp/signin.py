"""Sign-in pages on this site, backed by the WorkOS User Management API (AUTH_MODE=oidc plus WORKOS_API_KEY).

People sign in, create an account, confirm their email and reset their password on this site's /login page
instead of the provider's hosted page. WorkOS stores the accounts and checks the passwords; this server passes
each request on and keeps only the signed-in identity in the session cookie. That identity is the same
(issuer, subject) pair the MCP access tokens carry, so the website and Claude open the same workspace.

Claude's connector can use the same page (WorkOS Standalone Connect, the "external login URI" setting): WorkOS
sends the person here with an external_auth_id, and after sign-in this server hands them back to WorkOS, which
shows its consent screen and finishes the connector's OAuth flow.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlencode

import httpx
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .config import settings

log = logging.getLogger(__name__)
API = "https://api.workos.com"
PAGE = Path(__file__).parent / "static" / "signin.html"
transport: httpx.AsyncBaseTransport | None = None  # tests put a mock WorkOS here
PENDING_SECONDS = 600  # WorkOS pending authentication tokens last 10 minutes
CONNECT_SECONDS = 900


class WorkOSError(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(f"WorkOS HTTP {status}")
        self.status = status
        self.body = body
        self.code = str(body.get("code") or body.get("error") or "")

    def messages(self) -> list[str]:
        """The provider's own explanations (for example, why a password is too weak)."""
        found = [e.get("message") for e in self.body.get("errors") or [] if isinstance(e, dict)]
        found.append(self.body.get("message") or self.body.get("error_description"))
        return [m for m in found if m]


async def _call(method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {settings.workos_api_key}"}
    async with httpx.AsyncClient(base_url=API, timeout=15, transport=transport, headers=headers) as client:
        resp = await client.request(method, path, json=json, params=params)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    if resp.status_code >= 400:
        raise WorkOSError(resp.status_code, body)
    return body


def _client(request: Request) -> dict:
    """Passed to WorkOS so its bot and brute-force protection sees the real visitor."""
    return {"ip_address": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent", "")[:512]}


async def _authenticate(request: Request, grant_type: str, **fields) -> dict:
    return await _call("POST", "/user_management/authenticate", json={
        "client_id": settings.workos_client_id, "client_secret": settings.workos_api_key,
        "grant_type": grant_type, **fields, **_client(request)})


# ---------------------------------------------------------------- limits on guessing and email sending


class _Window:
    """At most `limit` uses per key within `seconds` (in memory, per server instance)."""

    def __init__(self, limit: int, seconds: int):
        self.limit, self.seconds = limit, seconds
        self.hits: dict[str, deque] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and now - q[0] > self.seconds:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self.hits) > 10_000:
                self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < self.seconds}
            return True


LIMITS = {
    "password": _Window(10, 600),  # sign-in attempts per address and per email
    "signup": _Window(5, 3600),
    "code": _Window(8, 600),  # email code attempts per pending sign-in
    "email": _Window(3, 900),  # emails sent (codes, reset links) per address and per email
}


def _allowed(kind: str, *keys: str) -> bool:
    return all(LIMITS[kind].allow(f"{kind}:{k}") for k in keys if k)


def _too_many() -> JSONResponse:
    return JSONResponse({"error": "Too many attempts. Wait a few minutes and try again."}, status_code=429)


# ---------------------------------------------------------------- session helpers


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    return origin is None or origin.rstrip("/") == settings.base_url.rstrip("/")


async def _json(request: Request) -> dict | Response:
    """JSON bodies only, from this site only: a plain cross-site form cannot post here."""
    if not request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"error": "Expected JSON."}, status_code=415)
    if not _origin_ok(request):
        return JSONResponse({"error": "Request from another site."}, status_code=403)
    try:
        data = await request.json()
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _text(data: dict, name: str, limit: int = 320) -> str:
    value = data.get(name)
    return value.strip()[:limit] if isinstance(value, str) else ""


def _name(user: dict) -> str | None:
    full = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    return full or None


def _connect_pending(request: Request) -> str | None:
    connect = request.session.get("connect")
    if connect and time.time() - connect.get("at", 0) < CONNECT_SECONDS:
        return connect["id"]
    return None


async def _connect_redirect(external_auth_id: str, user_id: str) -> str:
    """Hand a signed-in person back to WorkOS to finish Claude's connector sign-in. WorkOS matches the user we
    send by external_id, so the account's external_id is set to its own WorkOS ID first; the access token then
    names the same account the website uses."""
    user = await _call("GET", f"/user_management/users/{user_id}")
    if user.get("external_id") != user_id:
        user = await _call("PUT", f"/user_management/users/{user_id}", json={"external_id": user_id})
    done = await _call("POST", "/authkit/oauth2/complete", json={
        "external_auth_id": external_auth_id,
        "user": {"id": user_id, "email": user.get("email"),
                 "first_name": user.get("first_name"), "last_name": user.get("last_name")},
    })
    return done["redirect_uri"]


async def _signed_in(request: Request, result: dict) -> str:
    """Start a fresh session for the authenticated user; returns where to send the browser next."""
    user = result["user"]
    connect = _connect_pending(request)
    request.session.clear()
    request.session["user"] = {"iss": settings.oidc_issuer, "sub": user["id"],
                               "email": user.get("email"), "name": _name(user)}
    if connect:
        try:
            return await _connect_redirect(connect, user["id"])
        except Exception as exc:
            log.warning("Claude connection not completed: %s", getattr(exc, "code", type(exc).__name__))
            return "/login?error=connect"
    return "/"


async def _outcome(request: Request, call) -> dict:
    """Run one authentication call: {"redirect": url} when signed in, {"step": "verify"} when WorkOS has emailed
    a code to confirm the address first."""
    try:
        result = await call()
    except WorkOSError as exc:
        if exc.code == "email_verification_required":
            request.session["pending"] = {"token": exc.body.get("pending_authentication_token"),
                                          "email": exc.body.get("email"), "at": int(time.time())}
            return {"step": "verify", "email": exc.body.get("email")}
        raise
    return {"redirect": await _signed_in(request, result)}


def _failed(exc: Exception, message: str, status: int = 400) -> JSONResponse:
    if isinstance(exc, WorkOSError):
        if exc.status == 429:
            return _too_many()
        log.info("sign-in step rejected: HTTP %s %s", exc.status, exc.code)
    else:
        log.warning("sign-in step failed: %s", type(exc).__name__)
        message, status = "The sign-in service could not be reached. Try again in a moment.", 502
    return JSONResponse({"error": message}, status_code=status)


# ---------------------------------------------------------------- pages and endpoints


async def login_page(request: Request) -> Response:
    external = request.query_params.get("external_auth_id")
    if external:
        request.session["connect"] = {"id": external[:512], "at": int(time.time())}
    if request.session.get("user") and not _connect_pending(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse(PAGE, headers={"Cache-Control": "no-store"})


async def reset_page(request: Request) -> Response:
    return FileResponse(PAGE, headers={"Cache-Control": "no-store"})


async def status(request: Request) -> Response:
    user = request.session.get("user")
    pending = request.session.get("pending")
    fresh = pending and time.time() - pending.get("at", 0) < PENDING_SECONDS
    return JSONResponse({"signed_in": bool(user), "email": user.get("email") if user else None,
                         "connect": bool(_connect_pending(request)),
                         "verify_email": pending.get("email") if fresh else None},
                        headers={"Cache-Control": "no-store"})


async def password(request: Request) -> Response:
    data = await _json(request)
    if isinstance(data, Response):
        return data
    email, secret = _text(data, "email").lower(), data.get("password")
    if not email or not isinstance(secret, str) or not secret:
        return JSONResponse({"error": "Enter your email and password."}, status_code=400)
    if not _allowed("password", _client(request)["ip_address"], email):
        return _too_many()
    try:
        return JSONResponse(await _outcome(
            request, lambda: _authenticate(request, "password", email=email, password=secret)))
    except Exception as exc:
        return _failed(exc, "The email or password is incorrect.", 401)


async def signup(request: Request) -> Response:
    data = await _json(request)
    if isinstance(data, Response):
        return data
    email, secret = _text(data, "email").lower(), data.get("password")
    if not email or not isinstance(secret, str) or not secret:
        return JSONResponse({"error": "Enter your email and a password."}, status_code=400)
    if not _allowed("signup", _client(request)["ip_address"]):
        return _too_many()
    try:
        await _call("POST", "/user_management/users", json={
            "email": email, "password": secret,
            "first_name": _text(data, "first_name", 100) or None, "last_name": _text(data, "last_name", 100) or None})
    except WorkOSError as exc:
        text = " ".join(exc.messages()).lower()
        if exc.status == 429:
            return _too_many()
        if "already" in text or "not available" in text or "exists" in text:
            return JSONResponse({"error": "An account with this email already exists. Sign in instead."},
                                status_code=409)
        return JSONResponse({"error": " ".join(exc.messages()) or "The account could not be created."}, status_code=400)
    except Exception as exc:
        return _failed(exc, "")
    try:  # the new account needs its email confirmed; this sends the code
        return JSONResponse(await _outcome(
            request, lambda: _authenticate(request, "password", email=email, password=secret)))
    except Exception as exc:
        return _failed(exc, "Your account was created. Sign in to continue.")


async def verify(request: Request) -> Response:
    data = await _json(request)
    if isinstance(data, Response):
        return data
    pending = request.session.get("pending")
    if not pending or time.time() - pending.get("at", 0) > PENDING_SECONDS:
        return JSONResponse({"error": "This code has expired. Sign in again to get a new one."}, status_code=400)
    code = "".join(ch for ch in _text(data, "code", 20) if ch.isdigit())
    if not code:
        return JSONResponse({"error": "Enter the code from the email."}, status_code=400)
    if not _allowed("code", pending["token"]):
        return _too_many()
    try:
        return JSONResponse(await _outcome(request, lambda: _authenticate(
            request, "urn:workos:oauth:grant-type:email-verification:code",
            code=code, pending_authentication_token=pending["token"])))
    except Exception as exc:
        return _failed(exc, "The code is incorrect or has expired.")


async def resend(request: Request) -> Response:
    data = await _json(request)
    if isinstance(data, Response):
        return data
    pending = request.session.get("pending")
    if not pending or not pending.get("email"):
        return JSONResponse({"error": "Sign in again to get a new code."}, status_code=400)
    if not _allowed("email", _client(request)["ip_address"], pending["email"]):
        return _too_many()
    try:
        found = await _call("GET", "/user_management/users", params={"email": pending["email"], "limit": 1})
        user = (found.get("data") or [None])[0]
        if user:
            await _call("POST", f"/user_management/users/{user['id']}/email_verification/send")
    except Exception as exc:
        return _failed(exc, "The code could not be sent. Try again in a moment.")
    return JSONResponse({"ok": True})


async def forgot(request: Request) -> Response:
    data = await _json(request)
    if isinstance(data, Response):
        return data
    email = _text(data, "email").lower()
    if not email:
        return JSONResponse({"error": "Enter your email."}, status_code=400)
    if not _allowed("email", _client(request)["ip_address"], email):
        return _too_many()
    try:
        await _call("POST", "/user_management/password_reset", json={"email": email})
    except WorkOSError as exc:  # same answer whether or not the account exists
        log.info("password reset not sent: HTTP %s %s", exc.status, exc.code)
    except Exception as exc:
        return _failed(exc, "")
    return JSONResponse({"ok": True})


async def reset(request: Request) -> Response:
    data = await _json(request)
    if isinstance(data, Response):
        return data
    token, secret = _text(data, "token", 512), data.get("password")
    if not token or not isinstance(secret, str) or not secret:
        return JSONResponse({"error": "Enter a new password."}, status_code=400)
    if not _allowed("password", _client(request)["ip_address"]):
        return _too_many()
    try:
        result = await _call("POST", "/user_management/password_reset/confirm",
                             json={"token": token, "new_password": secret})
    except WorkOSError as exc:
        if exc.status == 429:
            return _too_many()
        text = " ".join(exc.messages())
        if "password" in text.lower() and "token" not in text.lower():
            return JSONResponse({"error": text}, status_code=400)
        return JSONResponse({"error": "This link has expired or was already used. Request a new one."}, status_code=400)
    except Exception as exc:
        return _failed(exc, "")
    return JSONResponse({"ok": True, "email": (result.get("user") or {}).get("email")})


async def google(request: Request) -> Response:
    state = secrets.token_urlsafe(24)
    request.session["oauth"] = {"state": state, "at": int(time.time())}
    params = {"response_type": "code", "client_id": settings.workos_client_id, "provider": "GoogleOAuth",
              "redirect_uri": f"{settings.base_url}/auth/callback", "state": state}
    return RedirectResponse(f"{API}/user_management/authorize?{urlencode(params)}", status_code=302)


async def callback(request: Request) -> Response:
    started = request.session.pop("oauth", None)
    q = request.query_params
    if not started or q.get("state") != started["state"] or time.time() - started["at"] > PENDING_SECONDS:
        return RedirectResponse("/login?error=expired", status_code=302)
    if "error" in q or not q.get("code"):
        return RedirectResponse("/login?error=cancelled", status_code=302)
    try:
        out = await _outcome(request, lambda: _authenticate(request, "authorization_code", code=q["code"]))
    except Exception as exc:
        log.warning("Google sign-in failed: %s", getattr(exc, "code", type(exc).__name__))
        return RedirectResponse("/login?error=failed", status_code=302)
    return RedirectResponse(out.get("redirect") or "/login?view=verify", status_code=302)


async def connect(request: Request) -> Response:
    """Already signed in on this site: continue Claude's connector sign-in with this account."""
    data = await _json(request)
    if isinstance(data, Response):
        return data
    user, external = request.session.get("user"), _connect_pending(request)
    if not user or not external:
        return JSONResponse({"error": "This connection request has expired. Start again from Claude."}, status_code=400)
    try:
        url = await _connect_redirect(external, user["sub"])
    except Exception as exc:
        return _failed(exc, "Claude could not be connected. Start again from Claude.")
    request.session.pop("connect", None)
    return JSONResponse({"redirect": url})


async def switch_account(request: Request) -> Response:
    """Sign out here but keep a pending Claude connection, so the next sign-in finishes it."""
    data = await _json(request)
    if isinstance(data, Response):
        return data
    request.session.pop("user", None)
    return JSONResponse({"ok": True})


def routes() -> list[Route]:
    return [
        Route("/login", login_page),
        Route("/reset-password", reset_page),
        Route("/auth/status", status),
        Route("/auth/password", password, methods=["POST"]),
        Route("/auth/signup", signup, methods=["POST"]),
        Route("/auth/verify", verify, methods=["POST"]),
        Route("/auth/resend", resend, methods=["POST"]),
        Route("/auth/forgot", forgot, methods=["POST"]),
        Route("/auth/reset", reset, methods=["POST"]),
        Route("/auth/google", google),
        Route("/auth/callback", callback),
        Route("/auth/connect", connect, methods=["POST"]),
        Route("/auth/switch", switch_account, methods=["POST"]),
    ]
