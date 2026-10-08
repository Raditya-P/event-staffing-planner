"""Small ASGI middlewares for a public deployment: per-client rate limits and security headers."""

from __future__ import annotations

import hashlib
import json
import threading
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

LIMITED_PREFIXES = ("/api/", "/mcp", "/login", "/auth/")
EXEMPT_SUFFIXES = ("/revision",)  # the dashboard polls this tiny endpoint every 2 seconds


class RateLimitMiddleware:
    """Token bucket per client: a signed-in Claude connection (by its token) or an address.
    In memory, so each server instance counts separately; good enough to stop one runaway client."""

    def __init__(self, app: ASGIApp, per_minute: int = 120):
        self.app = app
        self.capacity = max(per_minute, 1)
        self.rate = self.capacity / 60.0
        self.buckets: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def _key(self, scope: Scope) -> str:
        for name, value in scope.get("headers", []):
            if name == b"authorization" and value.lower().startswith(b"bearer "):
                return "t:" + hashlib.sha256(value[7:]).hexdigest()[:24]
        client = scope.get("client") or ("unknown", 0)
        return "a:" + str(client[0])

    def _allow(self, key: str) -> float:
        """0 if allowed, else seconds until a token is available."""
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.get(key, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1:
                self.buckets[key] = (tokens - 1, now)
                if len(self.buckets) > 10_000:  # forget idle clients
                    self.buckets = {k: v for k, v in self.buckets.items() if now - v[1] < 120}
                return 0.0
            self.buckets[key] = (tokens, now)
            return (1 - tokens) / self.rate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(LIMITED_PREFIXES) or path.endswith(EXEMPT_SUFFIXES):
            await self.app(scope, receive, send)
            return
        wait = self._allow(self._key(scope))
        if wait <= 0:
            await self.app(scope, receive, send)
            return
        body = json.dumps({"error": "Too many requests; slow down and try again shortly."}).encode()
        await send({"type": "http.response.start", "status": 429, "headers": [
            (b"content-type", b"application/json"), (b"retry-after", str(int(wait) + 1).encode()),
            (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


PAGE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class SecurityHeadersMiddleware:
    """Baseline browser protections. The strict CSP applies to our own HTML pages, not to /dev tools."""

    def __init__(self, app: ASGIApp, hsts: bool = False):
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _ in headers}
                extra = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"strict-origin-when-cross-origin")]
                ctype = dict((k.lower(), v) for k, v in headers).get(b"content-type", b"")
                if ctype.startswith(b"text/html") and not path.startswith("/dev"):
                    extra += [(b"content-security-policy", PAGE_CSP.encode()), (b"x-frame-options", b"DENY")]
                if self.hsts:
                    extra.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                headers += [(k, v) for k, v in extra if k not in names]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
