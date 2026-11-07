import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security import RateLimiter, SecurityMiddleware, client_key
from tests.conftest import make_settings


def build_app(tmp_path, password: str) -> TestClient:
    app = FastAPI()
    app.add_middleware(SecurityMiddleware, settings=make_settings(tmp_path, app_password=password, app_username="staff"))

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.get("/api/private")
    async def private():
        return {"secret": True}

    return TestClient(app)


def test_no_password_means_open_access_with_security_headers(tmp_path):
    client = build_app(tmp_path, "")
    r = client.get("/api/private")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_login_required_when_password_set(tmp_path):
    from app.security import SESSION_COOKIE, make_session

    settings = make_settings(tmp_path, app_password="s3cret", app_username="staff")
    client = build_app(tmp_path, "s3cret")
    r = client.get("/api/private")
    assert r.status_code == 401 and r.json() == {"detail": "Login required."}
    page = client.get("/some/page", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"].startswith("/login?next=")
    client.cookies.set(SESSION_COOKIE, "9999999999.forged")
    assert client.get("/api/private").status_code == 401
    client.cookies.set(SESSION_COOKIE, make_session(settings))
    assert client.get("/api/private").status_code == 200
    assert client.get("/api/health").status_code == 200  # health stays public


def test_rate_limiter_window():
    limiter = RateLimiter(per_hour=2)
    assert limiter.check("a") is None
    assert limiter.check("a") is None
    wait = limiter.check("a")
    assert wait is not None and wait > 0
    assert limiter.check("b") is None  # per-client
    assert RateLimiter(per_hour=0).check("a") is None  # disabled


def test_client_key_respects_proxy_setting():
    headers = {"x-forwarded-for": "203.0.113.9, 10.0.0.1"}
    assert client_key(("10.0.0.1", 1234), headers, trust_proxy=False) == "10.0.0.1"
    assert client_key(("10.0.0.1", 1234), headers, trust_proxy=True) == "203.0.113.9"
    assert client_key(None, {}, trust_proxy=False) == "unknown"
