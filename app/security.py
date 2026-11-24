"""Access control (login page + signed session cookie), per-client rate limiting and security headers."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from urllib.parse import quote

from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings
from app.site_switch import CLOSED_DETAIL, CLOSED_STATUS, SiteSwitch
from app.sync import PATH as SYNC_PATH
from app.sync import token_ok

SESSION_COOKIE = "jhs_session"
SESSION_DAYS = 30
PUBLIC_PATHS = frozenset({"/api/health", "/login", "/logout"})
API_PREFIXES = ("/api/", "/files/")
# While the site is closed (the dashboard's switch), these still answer everyone: the login page, so the studio login
# can always get in (a demo sign-in is refused there), signing out, and the health check. (The Mac's report, a POST to
# /api/sync/report with the sync token, needs no session and is taken while closed too: see _sync_report.)
OPEN_WHEN_CLOSED = PUBLIC_PATHS

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
]


CLIENT_ROLE = "client:"  # a client login's role is "client:<slug>"


@dataclass(frozen=True)
class ClientLogin:
    """A client page's own login (2026-11-22, app/clients.py, set in client_assets/clients.json): it may use only its
    page /client_<slug>, the calls that page makes and the jobs it made itself."""
    slug: str
    username: str
    password: str = field(repr=False)  # never in a log or a traceback


def client_of(role: str | None) -> str | None:
    """The client page a role belongs to ("client:<slug>" -> "<slug>"), else None."""
    return role[len(CLIENT_ROLE):] if role and role.startswith(CLIENT_ROLE) else None


def _session_key(settings: Settings, role: str = "full", login: ClientLogin | None = None) -> bytes:
    # Derived from the credentials, so changing a password signs out everyone who used it.
    if login is not None:
        material = f"jersey-hd-studio|client|{login.slug}|{login.username}|{login.password}"
    elif role == "demo":
        material = f"jersey-hd-studio|demo|{settings.demo_username}|{settings.demo_password}"
    elif role == "partner":
        material = f"jersey-hd-studio|partner|{settings.partner_username}|{settings.partner_password}"
    elif role == "full":  # unchanged, so studio sessions made before the demo login stay valid
        material = f"jersey-hd-studio|{settings.app_username}|{settings.app_password}"
    else:  # a client role without its login: refuse, rather than fall back to the studio's key
        raise ValueError(f"no session key for the role {role!r}")
    return hashlib.sha256(material.encode()).digest()


def make_session(settings: Settings, role: str = "full", login: ClientLogin | None = None) -> str:
    """A session token for the studio login, the demo login, or (with `login`) a client login."""
    payload = f"{int(time.time()) + SESSION_DAYS * 86400}"
    signature = hmac.new(_session_key(settings, role, login), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _signed(settings: Settings, token: str, role: str, login: ClientLogin | None = None) -> bool:
    expires, _, signature = token.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    expected = hmac.new(_session_key(settings, role, login), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def valid_session(settings: Settings, token: str) -> bool:
    return _signed(settings, token, "full")


def session_role(settings: Settings, token: str, logins: tuple[ClientLogin, ...] = ()) -> str | None:
    """'full' (the studio login), 'demo' (the demo login: /customerep only), 'partner' (the Partner dashboard only),
    'client:<slug>' (a client login: its own page only) or None (not signed in)."""
    if _signed(settings, token, "full"):
        return "full"
    if settings.demo_password and _signed(settings, token, "demo"):
        return "demo"
    if settings.partner_password and _signed(settings, token, "partner"):
        return "partner"
    for login in logins:
        if _signed(settings, token, "client", login):
            return CLIENT_ROLE + login.slug
    return None


def check_credentials(settings: Settings, username: str, password: str) -> bool:
    user_ok = secrets.compare_digest(username.encode(), settings.app_username.encode())
    pwd_ok = secrets.compare_digest(password.encode(), settings.app_password.encode())
    return user_ok and pwd_ok


def _same(given: str, expected: str) -> bool:
    return secrets.compare_digest(given.encode(), expected.encode())


def credentials_role(settings: Settings, username: str, password: str,
                     logins: tuple[ClientLogin, ...] = ()) -> str | None:
    """Which login these are: 'full', 'demo' / 'partner' (when their passwords are set), 'client:<slug>' or None."""
    full = check_credentials(settings, username, password)
    demo = _same(username, settings.demo_username) and _same(password, settings.demo_password)
    partner = _same(username, settings.partner_username) and _same(password, settings.partner_password)
    if full:
        return "full"
    if demo and settings.demo_password:
        return "demo"
    if partner and settings.partner_password:
        return "partner"
    for login in logins:
        if _same(username, login.username) and _same(password, login.password):
            return CLIENT_ROLE + login.slug
    return None


# What the demo page and the client pages call besides the page itself: its files and its API calls. The routes also
# check that a job was made with the same login (Job.demo, Job.client; main.login_scope), so other jobs and their
# files answer 404.
PAGE_ROUTES = (
    ("GET", re.compile(r"/static/.+")),
    ("GET", re.compile(r"/api/config")),
    ("GET", re.compile(r"/api/tokens/balance")),  # a client page's purse (the demo login just gets "no purse")
    ("GET", re.compile(r"/api/tokens/mine")),  # a client page's "Customer dashboard" tab (likewise)
    ("POST", re.compile(r"/api/pay/requests")),  # "Buy tokens" there: a bKash payment to be checked (client logins)
    ("POST", re.compile(r"/api/recreate")),
    ("POST", re.compile(r"/api/design")),
    ("GET", re.compile(r"/api/jobs/[^/]+")),
    ("POST", re.compile(r"/api/jobs/[^/]+/variants/\d+/(vectorize|retry)")),
    ("GET", re.compile(r"/api/jobs/[^/]+/panels-(eps|png)\.zip")),
    ("POST", re.compile(r"/api/jobs/[^/]+/downloads")),  # the downloads box: make a download's files, and how far
    ("GET", re.compile(r"/api/jobs/[^/]+/downloads")),
    ("GET", re.compile(r"/files/[^/]+/[^/]+")),
)
# What the demo login may reach: the demo page /customerep and what it calls (not the client pages).
DEMO_ROUTES = (("GET", re.compile(r"/customerep/?")), *PAGE_ROUTES)


def client_routes(slug: str) -> tuple:
    """What a client login may reach: its own page /client_<slug> and what it calls; no other page of the app. Since
    2026-11-24 also the list of its own jobs, for its page's History tab (the route gives it no one else's)."""
    return (("GET", re.compile(rf"/client_{re.escape(slug)}/?")), ("GET", re.compile(r"/api/jobs")), *PAGE_ROUTES)


def _allows(routes: tuple, method: str, path: str) -> bool:
    method = "GET" if method == "HEAD" else method
    return any(method == m and pattern.fullmatch(path) for m, pattern in routes)


def demo_allows(method: str, path: str) -> bool:
    return _allows(DEMO_ROUTES, method, path)


# What the partner login may reach: the Partner dashboard (2026-11-23; /client-dashboard is its old address), its data,
# and the preview pictures of client jobs (main.py's files route allows only previews of jobs made with a client
# login). Since 2026-11-24 it also checks bKash payments and adds tokens (it can't take tokens back: the route says so).
PARTNER_PAGE = "/partner-dashboard"
PARTNER_ROUTES = (
    ("GET", re.compile(r"/(partner|client)-dashboard/?")),
    ("GET", re.compile(r"/static/.+")),
    ("GET", re.compile(r"/api/tokens/clients")),
    ("GET", re.compile(r"/api/tokens/clients/[a-z0-9]+")),
    ("POST", re.compile(r"/api/tokens/clients/[a-z0-9]+/topup")),
    ("GET", re.compile(r"/api/tokens/jobs")),
    ("GET", re.compile(r"/api/tokens/topups")),
    ("GET", re.compile(r"/api/pay/requests")),
    ("POST", re.compile(r"/api/pay/requests/[a-z0-9]+/(approve|reject)")),
    ("POST", re.compile(r"/api/clients(/preview)?")),  # the Customer EntryPoint Builder (app/customers.py)
    ("POST", re.compile(r"/api/clients/[a-z0-9]+")),  # … and a client's name, phone and password changed afterwards
    # its History tab (2026-11-24): the clients' jobs with their files, downloads and exports; the routes keep it to
    # jobs made with a client login and send it no API cost, prompt or call log. No deleting, no generating again.
    ("GET", re.compile(r"/api/config")),
    ("GET", re.compile(r"/api/jobs")),
    ("GET", re.compile(r"/api/jobs/[^/]+")),
    ("POST", re.compile(r"/api/jobs/[^/]+/variants/\d+/vectorize")),
    ("GET", re.compile(r"/api/jobs/[^/]+/panels-(eps|png)\.zip")),
    ("POST", re.compile(r"/api/jobs/[^/]+/downloads")),
    ("GET", re.compile(r"/api/jobs/[^/]+/downloads")),
    ("GET", re.compile(r"/files/[^/]+/[^/]+")),
)


def partner_allows(method: str, path: str) -> bool:
    return _allows(PARTNER_ROUTES, method, path)


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

    def __init__(self, app: ASGIApp, settings: Settings, site: SiteSwitch | None = None,
                 logins: tuple[ClientLogin, ...] | dict[str, ClientLogin] = ()) -> None:
        self.app = app
        self.settings = settings
        self.site = site  # the dashboard's open/closed switch (None: always open)
        # The client pages' own logins (app/clients.py): a tuple, or the app's own dict (slug -> login), which is
        # read at every request, so a customer made on a dashboard (app/customers.py) can sign in at once.
        self._logins = logins
        self._routes: dict[str, tuple] = {}

    @property
    def logins(self) -> tuple[ClientLogin, ...]:
        return tuple(self._logins.values()) if isinstance(self._logins, dict) else tuple(self._logins)

    def _client_routes(self, slug: str) -> tuple:
        """What the client login of /client_<slug> may reach (nothing for a slug without a login now)."""
        if not any(login.slug == slug for login in self.logins):
            return ()
        return self._routes.setdefault(slug, client_routes(slug))

    def _sync_report(self, scope: Scope, path: str) -> bool:
        """The Mac's report (app/sync.py): a POST to the sync path with the shared token needs no session. Without
        the token it is like any other path (401 signed out, the closed site's 404), so it doesn't stand out."""
        if path != SYNC_PATH or scope.get("method") != "POST" or not self.settings.sync_token:
            return False
        header = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"authorization"), None)
        return token_ok(self.settings.sync_token, header)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if self._sync_report(scope, path):
            scope["jhs_role"] = None

            async def send_sync(message: dict) -> None:
                if message["type"] == "http.response.start":
                    message["headers"] = list(message.get("headers", [])) + SECURITY_HEADERS
                await send(message)

            await self.app(scope, receive, send_sync)
            return
        role = session_role(self.settings, _session_cookie(scope), self.logins) if self.settings.app_password else "full"
        scope["jhs_role"] = role  # the routes limit the demo login and the client logins to their own jobs
        if self.site is not None and self.site.closed and role != "full" and path not in OPEN_WHEN_CLOSED:
            await self._send_closed(send, path)
            return
        blocked = None
        if self.settings.app_password and path not in PUBLIC_PATHS:
            if role is None:
                blocked = (401, "Login required.", f"/login?next={quote(path, safe='')}")
            elif role == "demo" and not demo_allows(scope.get("method", "GET"), path):
                blocked = (403, "The demo login can only use the demo page.", "/customerep")
            elif role == "partner" and not partner_allows(scope.get("method", "GET"), path):
                blocked = (403, "This login can only use the partner dashboard.", PARTNER_PAGE)
            elif (client := client_of(role)) is not None and not _allows(
                    self._client_routes(client), scope.get("method", "GET"), path):
                blocked = (403, "This login can only use its own page.", f"/client_{client}")
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
