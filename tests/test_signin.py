"""The site's own sign-in pages (signin.py) against a stand-in for the WorkOS API."""

import dataclasses
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from forecast_mcp import config, signin

ISS = "https://issuer.test"
BASE = "https://planner.test"
COMPLETE = "https://issuer.test/oauth/authorize/complete?state=s1"


class FakeWorkOS:
    """Just enough of the User Management API: one verified user, sign-ups that need an email code."""

    def __init__(self):
        self.calls = []
        self.users = {"user_alice": {"id": "user_alice", "email": "alice@example.org", "first_name": "Alice",
                                     "last_name": "Ng", "external_id": None, "verified": True}}
        self.passwords = {"alice@example.org": "a long enough password"}

    def by_email(self, email):
        return next((u for u in self.users.values() if u["email"] == email), None)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path, method = request.url.path, request.method
        self.calls.append((method, path, body, dict(request.url.params)))
        if path == "/user_management/authenticate":
            assert body["client_id"] == "client_env" and body["client_secret"] == "sk_test"
            grant = body["grant_type"]
            if grant == "password":
                user = self.by_email(body["email"])
                if not user or self.passwords.get(body["email"]) != body["password"]:
                    return httpx.Response(400, json={"code": "invalid_credentials", "message": "Invalid credentials."})
                if not user["verified"]:
                    return httpx.Response(403, json={"code": "email_verification_required", "email": user["email"],
                                                     "pending_authentication_token": "pat_" + user["id"]})
                return httpx.Response(200, json={"user": user})
            if grant.endswith("email-verification:code"):
                user = self.users[body["pending_authentication_token"].removeprefix("pat_")]
                if body["code"] != "123456":
                    return httpx.Response(400, json={"code": "invalid_one_time_code"})
                user["verified"] = True
                return httpx.Response(200, json={"user": user})
            if grant == "authorization_code" and body["code"] == "good":
                return httpx.Response(200, json={"user": self.users["user_alice"]})
            return httpx.Response(400, json={"error": "invalid_grant"})
        if path == "/user_management/users" and method == "POST":
            if self.by_email(body["email"]):
                return httpx.Response(422, json={"code": "user_creation_error",
                                                 "errors": [{"code": "email_not_available",
                                                             "message": "This email is not available."}]})
            if len(body["password"]) < 10:
                return httpx.Response(422, json={"code": "password_strength_error",
                                                 "message": "Password does not meet strength requirements."})
            user = {"id": "user_new", "email": body["email"], "first_name": body.get("first_name"),
                    "last_name": body.get("last_name"), "external_id": None, "verified": False}
            self.users[user["id"]], self.passwords[user["email"]] = user, body["password"]
            return httpx.Response(201, json=user)
        if path == "/user_management/users" and method == "GET":
            user = self.by_email(request.url.params["email"])
            return httpx.Response(200, json={"data": [user] if user else []})
        if path.startswith("/user_management/users/"):
            user_id = path.split("/")[3]
            if path.endswith("/email_verification/send"):
                return httpx.Response(200, json=self.users[user_id])
            if method == "PUT":
                self.users[user_id].update(body)
            return httpx.Response(200, json=self.users[user_id])
        if path == "/user_management/password_reset":
            return httpx.Response(201 if self.by_email(body["email"]) else 404, json={})
        if path == "/user_management/password_reset/confirm":
            if body["token"] != "reset-token":
                return httpx.Response(400, json={"code": "password_reset_token_expired", "message": "Token expired."})
            self.passwords["alice@example.org"] = body["new_password"]
            return httpx.Response(200, json={"user": self.users["user_alice"]})
        if path == "/authkit/oauth2/complete":
            return httpx.Response(200, json={"redirect_uri": COMPLETE})
        return httpx.Response(404, json={})


@pytest.fixture
def workos(monkeypatch):
    fake = FakeWorkOS()
    cfg = dataclasses.replace(config.settings, auth_mode="oidc", oidc_issuer=ISS, public_base_url=BASE,
                              workos_api_key="sk_test", workos_client_id="client_env", session_secret="s" * 40)
    monkeypatch.setattr(signin, "settings", cfg)
    monkeypatch.setattr(signin, "transport", httpx.MockTransport(fake))
    monkeypatch.setattr(signin, "LIMITS", {k: signin._Window(w.limit, w.seconds) for k, w in signin.LIMITS.items()})
    return fake


@pytest.fixture
def client(workos):
    async def whoami(request):
        return JSONResponse(request.session.get("user"))

    app = Starlette(routes=signin.routes() + [Route("/whoami", whoami)])
    app.add_middleware(SessionMiddleware, secret_key="s" * 40, session_cookie="fm_session")
    with TestClient(app, base_url=BASE) as c:
        yield c


def test_password_sign_in_gives_the_identity_claude_uses(client, workos):
    r = client.post("/auth/password", json={"email": "Alice@Example.org ", "password": "a long enough password"})
    assert r.status_code == 200 and r.json() == {"redirect": "/"}
    # The same (issuer, subject) as the MCP access tokens, so the website and Claude share one workspace.
    assert client.get("/whoami").json() == {"iss": ISS, "sub": "user_alice", "email": "alice@example.org",
                                            "name": "Alice Ng"}
    sent = workos.calls[0][2]
    assert sent["email"] == "alice@example.org" and sent["ip_address"] and "user_agent" in sent


def test_a_wrong_password_gets_one_message_for_every_cause(client):
    for email in ("alice@example.org", "nobody@example.org"):
        r = client.post("/auth/password", json={"email": email, "password": "wrong password"})
        assert r.status_code == 401 and r.json()["error"] == "The email or password is incorrect."
    assert client.get("/whoami").json() is None


def test_a_new_account_confirms_its_email_with_a_code(client):
    taken = client.post("/auth/signup", json={"email": "alice@example.org", "password": "another long password"})
    assert taken.status_code == 409 and "already exists" in taken.json()["error"]
    weak = client.post("/auth/signup", json={"email": "bo@example.org", "password": "short"})
    assert weak.status_code == 400 and "strength" in weak.json()["error"]

    r = client.post("/auth/signup", json={"email": "bo@example.org", "password": "a fine long password",
                                          "first_name": "Bo"})
    assert r.json() == {"step": "verify", "email": "bo@example.org"}
    assert client.get("/auth/status").json()["verify_email"] == "bo@example.org"
    assert client.post("/auth/resend", json={}).json() == {"ok": True}
    assert client.post("/auth/verify", json={"code": "000000"}).status_code == 400
    done = client.post("/auth/verify", json={"code": "123 456"})
    assert done.json() == {"redirect": "/"} and client.get("/whoami").json()["sub"] == "user_new"


def test_password_reset_does_not_reveal_which_emails_have_accounts(client, workos):
    for email in ("alice@example.org", "nobody@example.org"):
        assert client.post("/auth/forgot", json={"email": email}).json() == {"ok": True}
    bad = client.post("/auth/reset", json={"token": "old", "password": "a brand new password"})
    assert bad.status_code == 400 and "expired" in bad.json()["error"]
    ok = client.post("/auth/reset", json={"token": "reset-token", "password": "a brand new password"})
    assert ok.json() == {"ok": True, "email": "alice@example.org"}
    assert client.post("/auth/password", json={"email": "alice@example.org",
                                                "password": "a brand new password"}).status_code == 200


def test_google_sign_in_goes_straight_to_google_and_checks_the_state(client):
    start = client.get("/auth/google", follow_redirects=False)
    url = urlparse(start.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "api.workos.com" and q["provider"] == "GoogleOAuth" and q["client_id"] == "client_env"
    assert q["redirect_uri"] == f"{BASE}/auth/callback"

    forged = client.get("/auth/callback", params={"code": "good", "state": "forged"}, follow_redirects=False)
    assert forged.headers["location"] == "/login?error=expired"
    state = parse_qs(urlparse(client.get("/auth/google", follow_redirects=False).headers["location"]).query)["state"][0]
    ok = client.get("/auth/callback", params={"code": "good", "state": state}, follow_redirects=False)
    assert ok.headers["location"] == "/" and client.get("/whoami").json()["sub"] == "user_alice"


def test_claude_connector_sign_in_is_handed_back_to_workos(client, workos):
    assert client.get("/login", params={"external_auth_id": "ext_1"}).status_code == 200
    r = client.post("/auth/password", json={"email": "alice@example.org", "password": "a long enough password"})
    assert r.json() == {"redirect": COMPLETE}
    complete = next(c for c in workos.calls if c[1] == "/authkit/oauth2/complete")[2]
    assert complete["external_auth_id"] == "ext_1" and complete["user"]["id"] == "user_alice"
    assert workos.users["user_alice"]["external_id"] == "user_alice"  # so WorkOS finds this same account

    # Already signed in: the page asks before connecting, then hands back again.
    assert client.get("/login", follow_redirects=False).headers["location"] == "/"
    assert client.get("/login", params={"external_auth_id": "ext_2"}).status_code == 200
    assert client.get("/auth/status").json()["connect"] is True
    assert client.post("/auth/connect", json={}).json() == {"redirect": COMPLETE}


def test_only_json_from_this_site_is_accepted(client):
    form = client.post("/auth/password", data={"email": "alice@example.org", "password": "x"})
    assert form.status_code == 415
    foreign = client.post("/auth/password", json={"email": "a@b.c", "password": "x"},
                          headers={"Origin": "https://evil.test"})
    assert foreign.status_code == 403


def test_repeated_guessing_is_slowed_down(client):
    codes = [client.post("/auth/password", json={"email": "alice@example.org", "password": f"guess {i}"}).status_code
             for i in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_the_page_follows_the_strict_content_policy(client):
    page = client.get("/login").text
    assert "/static/signin.js" in page and "<script>" not in page and "Continue with Google" in page


def test_own_sign_in_needs_no_oauth_client_of_its_own():
    cfg = dataclasses.replace(config.settings, auth_mode="oidc", oidc_issuer=ISS, public_base_url=BASE,
                              session_secret="s" * 40, oidc_client_id="", oidc_client_secret="",
                              workos_api_key="sk_test", workos_client_id="client_env")
    assert cfg.own_signin and not [p for p in cfg.problems() if "needs" in p]
    hosted = dataclasses.replace(cfg, workos_api_key="", workos_client_id="")
    assert not hosted.own_signin and "AUTH_MODE=oidc needs OIDC_CLIENT_ID." in hosted.problems()
