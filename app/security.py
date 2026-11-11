"""Access control (login page + signed session cookie), per-client rate limiting and security headers."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from collections import defaultdict, deque
from http.cookies import CookieError, SimpleCookie
from urllib.parse import quote

from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings
from app.site_switch import CLOSED_DETAIL, CLOSED_STATUS, SiteSwitch

SESSION_COOKIE = "jhs_session"
SESSION_DAYS = 30
PUBLIC_PATHS = frozenset({"/api/health", "/login", "/logout"})
API_PREFIXES = ("/api/", "/files/")
# While the site is closed (the dashboard's switch), these still answer everyone: the login page, so the studio login
# can always get in (a demo sign-in is refused there), signing out, and the health check.
OPEN_WHEN_CLOSED = PUBLIC_PATHS

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
]


def _session_key(settings: Settings, role: str = "full") -> bytes:
    # Derived from the credentials, so changing a password signs out everyone who used it.
    if role == "demo":
        material = f"jersey-hd-studio|demo|{settings.demo_username}|{settings.demo_password}"
    else:  # unchanged, so studio sessions made before the demo login stay valid
        material = f"jersey-hd-studio|{settings.app_username}|{settings.app_password}"
    return hashlib.sha256(material.encode()).digest()


def make_session(settings: Settings, role: str = "full") -> str:
    payload = f"{int(time.time()) + SESSION_DAYS * 86400}"
    signature = hmac.new(_session_key(settings, role), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _signed(settings: Settings, token: str, role: str) -> bool:
    expires, _, signature = token.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    expected = hmac.new(_session_key(settings, role), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def valid_session(settings: Settings, token: str) -> bool:
    return _signed(settings, token, "full")


def session_role(settings: Settings, token: str) -> str | None:
    """'full' (the studio login), 'demo' (the demo login: /customerep only) or None (not signed in)."""
    if _signed(settings, token, "full"):
        return "full"
    if settings.demo_password and _signed(settings, token, "demo"):
        return "demo"
    return None


def check_credentials(settings: Settings, username: str, password: str) -> bool:
    user_ok = secrets.compare_digest(username.encode(), settings.app_username.encode())
    pwd_ok = secrets.compare_digest(password.encode(), settings.app_password.encode())
    return user_ok and pwd_ok


def credentials_role(settings: Settings, username: str, password: str) -> str | None:
    """Which login these are: 'full', 'demo' (when a demo password is set) or None."""
    full = check_credentials(settings, username, password)
    demo = (
        secrets.compare_digest(username.encode(), settings.demo_username.encode())
        and secrets.compare_digest(password.encode(), settings.demo_password.encode())
    )
    if full:
        return "full"
    return "demo" if demo and settings.demo_password else None


# What the demo login may reach: the demo page, its files and the calls it makes. The routes also check that a job
# was made with the demo login (Job.demo), so other jobs and their files answer 404.
DEMO_ROUTES = (
    ("GET", re.compile(r"/customerep/?")),
    ("GET", re.compile(r"/static/.+")),
    ("GET", re.compile(r"/api/config")),
    ("POST", re.compile(r"/api/recreate")),
    ("POST", re.compile(r"/api/design")),
    ("GET", re.compile(r"/api/jobs/[^/]+")),
    ("POST", re.compile(r"/api/jobs/[^/]+/variants/\d+/(vectorize|retry)")),
    ("GET", re.compile(r"/api/jobs/[^/]+/panels-(eps|png)\.zip")),
    ("GET", re.compile(r"/files/[^/]+/[^/]+")),
)


def demo_allows(method: str, path: str) -> bool:
    method = "GET" if method == "HEAD" else method
    return any(method == m and pattern.fullmatch(path) for m, pattern in DEMO_ROUTES)


def _session_cookie(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            try:
                morsel = SimpleCookie(value.decode("latin-1")).get(SESSION_COOKIE)
            except CookieError:
                return ""
            return morsel.value if morsel else ""
    return ""


class SecurityMiddleware:
    """Pure-ASGI middleware: login required when APP_PASSWORD is set, plus security headers."""

    def __init__(self, app: ASGIApp, settings: Settings, site: SiteSwitch | None = None) -> None:
        self.app = app
        self.settings = settings
        self.site = site  # the dashboard's open/closed switch (None: always open)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        role = session_role(self.settings, _session_cookie(scope)) if self.settings.app_password else "full"
        scope["jhs_role"] = role  # the routes limit the demo login to its own jobs
        if self.site is not None and self.site.closed and role != "full" and path not in OPEN_WHEN_CLOSED:
            await self._send_closed(send, path)
            return
        blocked = None
        if self.settings.app_password and path not in PUBLIC_PATHS:
            if role is None:
                blocked = (401, "Login required.", f"/login?next={quote(path, safe='')}")
            elif role == "demo" and not demo_allows(scope.get("method", "GET"), path):
                blocked = (403, "The demo login can only use the demo page.", "/customerep")
        if blocked:
            code, detail, target = blocked
            if path.startswith(API_PREFIXES):
                status, body = code, json.dumps({"detail": detail}).encode()
                headers = [(b"content-type", b"application/json")]
            else:
                status, body = 303, b""
                headers = [(b"location", target.encode("latin-1"))]
            await send({"type": "http.response.start", "status": status, "headers": headers + SECURITY_HEADERS})
            await send({"type": "http.response.body", "body": body})
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + SECURITY_HEADERS
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _send_closed(send: Send, path: str) -> None:
        """The site is closed: a plain 404 with an empty body for pages (the browser shows its own "can't be found"
        screen), and FastAPI's own 404 JSON for the API and files. no-store, so nothing is remembered once reopened."""
        if path.startswith(API_PREFIXES):
            body = json.dumps({"detail": CLOSED_DETAIL}, separators=(",", ":")).encode()  # byte for byte FastAPI's
            headers = [(b"content-type", b"application/json")]
        else:
            body = b""
            headers = []
        headers += [(b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]
        await send({"type": "http.response.start", "status": CLOSED_STATUS, "headers": headers + SECURITY_HEADERS})
        await send({"type": "http.response.body", "body": body})


class RateLimiter:
    """Sliding one-hour window of request timestamps per client key (in memory)."""

    WINDOW_S = 3600.0

    def __init__(self, per_hour: int) -> None:
        self.per_hour = per_hour
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, cost: int = 1) -> float | None:
        """Record `cost` hits for `key`. Returns None if allowed, else seconds until a slot frees up."""
        if self.per_hour <= 0:
            return None
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] >= self.WINDOW_S:
            hits.popleft()
        if len(hits) + cost > self.per_hour:
            return max(1.0, self.WINDOW_S - (now - hits[0])) if hits else self.WINDOW_S
        hits.extend([now] * cost)
        return None


def client_key(scope_client: tuple[str, int] | None, headers: dict[str, str], trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return scope_client[0] if scope_client else "unknown"
