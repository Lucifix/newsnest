import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.auth


@pytest.fixture
def app(env, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    from newsnest.web import app
    return app


def test_no_password_locks_everything(app, monkeypatch):
    monkeypatch.delenv("NEWSNEST_PASSWORD", raising=False)
    with TestClient(app) as c:
        assert c.get("/", follow_redirects=False).status_code == 503
        assert c.post("/api/read", data={"ids": "1"}).status_code == 503
        assert c.get("/health").json().keys() == {"status"}


def test_login_flow_and_api_protection(app, monkeypatch):
    monkeypatch.setenv("NEWSNEST_PASSWORD", "s3cret")
    with TestClient(app) as c:
        r = c.get("/settings", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/settings"
        assert c.get("/settings/feeds", follow_redirects=False).headers["location"] == "/login?next=/settings/feeds"
        for path in ("/api/read", "/api/saved", "/api/feedback", "/api/run", "/settings/feed_add", "/discovery/starter", "/missed"):
            assert c.post(path, data={}).status_code == 401, path
        assert c.get("/feed/more?token=x&offset=0").status_code == 401
        assert c.get("/go/1", follow_redirects=False).status_code == 303
        assert c.get("/static/app.css").status_code == 200 and c.get("/manifest.webmanifest").status_code == 200
        assert c.post("/login", data={"password": "wrong"}).status_code == 401
        r = c.post("/login", data={"password": "s3cret", "next": "/settings"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/settings"
        assert c.get("/settings").status_code == 200
        assert "sources" in c.get("/health").json()  # details once logged in
        c.get("/logout")
        assert c.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("host", ["x/login?", "x/static/", "x/health#", "x/offline?"])
def test_forged_host_header_does_not_skip_login(app, monkeypatch, host):
    # request.url is rebuilt from the Host header; the login check must use the routed path instead
    monkeypatch.setenv("NEWSNEST_PASSWORD", "s3cret")
    with TestClient(app) as c:
        assert c.get("/settings/backup/database", headers={"host": host}, follow_redirects=False).status_code == 303
        assert c.post("/api/run", headers={"host": host}).status_code == 401


def test_non_ascii_password(app, monkeypatch):
    monkeypatch.setenv("NEWSNEST_PASSWORD", "šifra-žaba")
    with TestClient(app) as c:
        assert c.post("/login", data={"password": "šifra"}).status_code == 401
        assert c.post("/login", data={"password": "šifra-žaba"}, follow_redirects=False).status_code == 303


def test_password_change_logs_out_and_next_is_local_only(app, monkeypatch):
    monkeypatch.setenv("NEWSNEST_PASSWORD", "one")
    with TestClient(app) as c:
        r = c.post("/login", data={"password": "one", "next": "//evil.example"}, follow_redirects=False)
        assert r.headers["location"] == "/"
        monkeypatch.setenv("NEWSNEST_PASSWORD", "two")
        assert c.get("/", follow_redirects=False).status_code == 303
