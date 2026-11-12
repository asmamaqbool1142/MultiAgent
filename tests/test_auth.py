import pytest
from fastapi.testclient import TestClient

from backend import auth, conversations, google_oauth


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_DB_PATH", tmp_path / "users.sqlite3")
    monkeypatch.setattr(conversations, "DB_PATH", tmp_path / "chat.sqlite3")
    monkeypatch.setattr(google_oauth, "LEGACY_TOKEN_PATH", tmp_path / "legacy.json")
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    auth._attempts.clear()
    from backend.main import app

    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


def signup(client, email="a@example.com", password="correct horse"):
    return client.post("/api/auth/signup", json={"email": email, "password": password})


def test_api_requires_login(client):
    assert client.get("/api/conversations").status_code == 401
    assert client.get("/app", follow_redirects=False).status_code == 307
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200


def test_signup_login_logout(client):
    assert signup(client).status_code == 200
    assert client.get("/api/auth/me").json()["user"]["email"] == "a@example.com"
    assert client.get("/app").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").json()["user"] is None
    assert client.get("/api/conversations").status_code == 401
    assert client.post("/api/auth/login", json={"email": "A@Example.com", "password": "wrong password"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "A@Example.com", "password": "correct horse"}).status_code == 200


def test_duplicate_and_weak_signup_rejected(client):
    assert signup(client).status_code == 200
    assert signup(client).status_code == 400
    assert signup(client, "b@example.com", "short").status_code == 400
    assert signup(client, "not-an-email").status_code == 400


def test_session_cookie_is_httponly(client):
    cookie = signup(client).headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_users_cannot_see_each_others_chats(client):
    signup(client, "a@example.com")
    chat_id = client.post("/api/conversations", json={"title": "secret"}).json()["id"]
    client.post("/api/auth/logout")
    signup(client, "b@example.com")
    assert client.get("/api/conversations").json() == []
    assert client.get(f"/api/conversations/{chat_id}").status_code == 404
    assert client.delete(f"/api/conversations/{chat_id}").status_code == 404


def test_cross_site_post_blocked(client):
    signup(client)
    response = client.post("/api/conversations", json={}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_invite_list(client, monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "@corp.example,boss@other.example")
    assert signup(client, "x@gmail.com").status_code == 400
    assert signup(client, "x@corp.example").status_code == 200


def test_login_throttle(client):
    signup(client)
    codes = [client.post("/api/auth/login", json={"email": "a@example.com", "password": "nope nope"}).status_code for _ in range(12)]
    assert codes[-1] == 401 and 401 in codes  # throttle raises AuthError, surfaced as 401
    assert "Too many" in client.post("/api/auth/login", json={"email": "a@example.com", "password": "nope nope"}).json()["detail"]


def test_first_user_inherits_legacy_data(client, tmp_path):
    (tmp_path / "legacy.json").write_text("{}")
    signup(client)
    assert not (tmp_path / "legacy.json").exists()
    user_id = auth.user_for_session(client.cookies.get(auth.SESSION_COOKIE))["id"]
    assert auth.load_google_token(user_id) == "{}"


def test_google_signin_takes_over_unverified_password_account(client):
    signup(client, "victim@example.com", "attacker knows this")
    user = auth.sign_in_with_google("victim@example.com", "sub-1", "Victim")
    assert auth.user_for_session(None) is None
    with pytest.raises(auth.AuthError):
        auth.authenticate("victim@example.com", "attacker knows this")
    assert user["email"] == "victim@example.com"
