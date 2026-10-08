"""Sign-in: token checks, one workspace per user, and the MCP endpoint's OAuth handshake.
Uses a locally generated signing key in place of a real identity provider."""

import dataclasses
import socket
import threading
import time

import httpx
import httpx2
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from forecast_mcp import accounts, auth, config, mcp_server
from forecast_mcp import services as S
from forecast_mcp.db import session_scope

ISS = "https://issuer.test"
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = _free_port()
BASE = f"http://127.0.0.1:{PORT}"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeProvider:
    issuer = ISS

    def metadata(self):
        return {"issuer": ISS}

    def decode(self, token, audience):
        return jwt.decode(token, KEY.public_key(), algorithms=["RS256"], issuer=ISS, audience=audience or None,
                          options={"verify_aud": bool(audience), "require": ["exp", "iss", "sub"]})


def token(sub="user-a", aud=f"{BASE}/mcp", iss=ISS, ttl=600, **extra):
    now = int(time.time())
    claims = {"iss": iss, "sub": sub, "aud": aud, "iat": now, "exp": now + ttl, "scope": "openid", **extra}
    return jwt.encode(claims, KEY, algorithm="RS256")


@pytest.fixture
def oidc(monkeypatch):
    cfg = dataclasses.replace(
        config.settings, auth_mode="oidc", oidc_issuer=ISS, public_base_url=BASE, mcp_audience=f"{BASE}/mcp",
        oidc_client_id="client", oidc_client_secret="secret", session_secret="s" * 40,
    )
    monkeypatch.setattr(auth, "_provider", FakeProvider())
    monkeypatch.setattr(auth, "settings", cfg)
    monkeypatch.setattr(mcp_server, "settings", cfg)
    return cfg


async def test_token_checks(oidc):
    verifier = auth.JwtTokenVerifier()
    good = await verifier.verify_token(token(email="a@example.org"))
    assert good is not None and good.subject == "user-a" and good.claims["email"] == "a@example.org"
    assert await verifier.verify_token(token(ttl=-120)) is None  # expired
    assert await verifier.verify_token(token(aud="https://someone-else/mcp")) is None  # not for us
    assert await verifier.verify_token(token(iss="https://evil.test")) is None  # wrong issuer
    assert await verifier.verify_token("not-a-jwt") is None


def test_each_user_gets_a_private_workspace():
    a1 = accounts.actor_for_identity(ISS, "alice", "alice@example.org", None, "dashboard")
    a2 = accounts.actor_for_identity(ISS, "alice", "alice@example.org", None, "chat")
    b = accounts.actor_for_identity(ISS, "bob", None, "Bob", "dashboard")
    assert a1.workspace_id == a2.workspace_id != b.workspace_id
    assert a1.name == "alice@example.org" and b.name == "Bob"
    with session_scope() as s:
        alice_events = {e["id"] for e in S.list_events(s, a1)}
        bob_events = {e["id"] for e in S.list_events(s, b)}
        assert len(alice_events) == 2 and alice_events.isdisjoint(bob_events)
        with pytest.raises(S.NotFound):
            S.event_detail(s, b, next(iter(alice_events)))
    with session_scope() as s:  # the demo copy was optimized right away
        off = S.official_scenario(s, next(iter(alice_events)))
        assert S.scenario_state(s, a1, off.id)["run"]["status"] == "done"


@pytest.fixture
def auth_server(oidc):
    server = mcp_server.build_server(
        auth=AuthSettings(issuer_url=ISS, resource_server_url=f"{BASE}/mcp", validate_token_resource=False),
        token_verifier=auth.JwtTokenVerifier(),
    )
    app = server.streamable_http_app(stateless_http=True, json_response=True,
                                     transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if uv.started:
            break
        time.sleep(0.05)
    yield
    uv.should_exit = True
    thread.join(timeout=10)


async def test_mcp_requires_a_token_and_points_to_the_provider(auth_server):
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{BASE}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                            headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 401
        assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]
        meta = (await http.get(f"{BASE}/.well-known/oauth-protected-resource/mcp")).json()
        assert meta["resource"] == f"{BASE}/mcp" and meta["authorization_servers"][0].rstrip("/") == ISS


async def _events_for(sub: str) -> set[str]:
    headers = {"Authorization": f"Bearer {token(sub=sub)}"}
    async with httpx2.AsyncClient(headers=headers, timeout=30) as http:
        async with Client(streamable_http_client(f"{BASE}/mcp", http_client=http)) as client:
            result = await client.call_tool("list_events", {})
            assert not result.is_error, result.content
            return {e["id"] for e in result.structured_content["events"]}


async def test_claude_sees_only_the_signed_in_users_workspace(auth_server):
    carol = await _events_for("carol")
    dave = await _events_for("dave")
    assert len(carol) == 2 and len(dave) == 2 and carol.isdisjoint(dave)
    assert carol == await _events_for("carol")  # same user, same workspace
