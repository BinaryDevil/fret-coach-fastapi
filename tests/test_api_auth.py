from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.security as security
from app.main import app


def test_health_and_security_headers(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.parametrize(
    ("access", "refresh", "message"),
    [
        ("short", "r" * 40, "at least 32"),
        ("same-secret-" * 4, "same-secret-" * 4, "must be different"),
    ],
)
def test_startup_rejects_weak_secrets(monkeypatch, access, refresh, message):
    monkeypatch.setattr(main_module, "JWT_ACCESS_SECRET", access)
    monkeypatch.setattr(main_module, "JWT_REFRESH_SECRET", refresh)
    with pytest.raises(RuntimeError, match=message):
        with TestClient(app):
            pass


class TestRegisterAndLogin:
    def test_register_returns_tokens_and_profile(self, register):
        body = register(email="New.Player@Example.com", display_name="Nova")
        assert body["accessToken"] and body["refreshToken"]
        assert body["user"]["email"] == "new.player@example.com"
        assert body["user"]["displayName"] == "Nova"
        assert body["user"]["handedness"] == "RIGHT"
        assert "passwordHash" not in body["user"]

    def test_register_creates_progress_row(self, client, user):
        progress = client.get("/api/v1/progress/me", headers=user["headers"]).json()
        assert progress["totalSessions"] == 0
        assert progress["userId"] == user["user"]["id"]

    def test_duplicate_email_is_case_insensitive(self, client, register):
        register(email="dup@example.com")
        response = client.post("/api/v1/auth/register", json={"email": "DUP@example.com", "password": "password1"})
        assert response.status_code == 409

    @pytest.mark.parametrize(
        "payload",
        [
            {"email": "not-an-email", "password": "password1"},
            {"email": "a@example.com", "password": "short"},
            {"email": "a@example.com", "password": "password1", "role": "admin"},
        ],
    )
    def test_register_validation(self, client, payload):
        assert client.post("/api/v1/auth/register", json=payload).status_code == 422

    def test_login(self, client, register):
        register(email="me@example.com", password="correct-horse")
        response = client.post("/api/v1/auth/login", json={"email": "ME@example.com", "password": "correct-horse"})
        assert response.status_code == 200
        assert response.json()["user"]["email"] == "me@example.com"

    @pytest.mark.parametrize("email,password", [("me@example.com", "wrong-horse"), ("nobody@example.com", "correct-horse")])
    def test_login_failures(self, client, register, email, password):
        register(email="me@example.com", password="correct-horse")
        response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid email or password"


class TestRefreshAndLogout:
    def test_refresh_issues_new_tokens(self, client, user):
        response = client.post("/api/v1/auth/refresh", json={"refreshToken": user["refreshToken"]})
        assert response.status_code == 200
        assert response.json()["user"]["id"] == user["user"]["id"]

    def test_access_token_is_not_a_refresh_token(self, client, user):
        response = client.post("/api/v1/auth/refresh", json={"refreshToken": user["accessToken"]})
        assert response.status_code == 401

    def test_logout_revokes_refresh_token(self, client, user):
        assert client.post("/api/v1/auth/logout", headers=user["headers"]).status_code == 204
        response = client.post("/api/v1/auth/refresh", json={"refreshToken": user["refreshToken"]})
        assert response.status_code == 401

    def test_logout_requires_auth(self, client):
        assert client.post("/api/v1/auth/logout").status_code == 401


class TestGoogleLogin:
    CREDENTIAL = "x" * 32

    @pytest.fixture
    def claims(self, monkeypatch):
        claims = {"sub": "google-1", "email": "G.User@example.com", "name": "G User"}
        monkeypatch.setattr(main_module, "verify_google_credential", lambda credential: claims)
        return claims

    def _login(self, client):
        return client.post("/api/v1/auth/google", json={"credential": self.CREDENTIAL})

    def test_creates_new_user(self, client, claims):
        response = self._login(client)
        assert response.status_code == 200
        assert response.json()["user"]["email"] == "g.user@example.com"
        assert response.json()["user"]["displayName"] == "G User"
        assert self._login(client).json()["user"]["id"] == response.json()["user"]["id"]

    def test_links_existing_email_account(self, client, register, claims):
        existing = register(email="g.user@example.com", display_name="Keep Me")
        response = self._login(client)
        assert response.json()["user"]["id"] == existing["user"]["id"]
        assert response.json()["user"]["displayName"] == "Keep Me"

    def test_rejects_email_linked_to_other_google_account(self, client, claims):
        self._login(client)
        claims["sub"] = "google-2"
        assert self._login(client).status_code == 409

    def test_invalid_credential(self, client, monkeypatch):
        def reject(credential):
            raise ValueError("Invalid Google credential audience")

        monkeypatch.setattr(main_module, "verify_google_credential", reject)
        response = self._login(client)
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid Google credential audience"

    def test_not_configured(self, client, monkeypatch):
        monkeypatch.setattr(security, "GOOGLE_CLIENT_ID", None)
        assert self._login(client).status_code == 503


class TestProfile:
    def test_requires_bearer_token(self, client):
        response = client.get("/api/v1/users/me")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_rejects_invalid_token(self, client):
        response = client.get("/api/v1/users/me", headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid access token"

    def test_refresh_token_cannot_access_api(self, client, user):
        headers = {"Authorization": f"Bearer {user['refreshToken']}"}
        assert client.get("/api/v1/users/me", headers=headers).status_code == 401

    def test_get_profile(self, client, user):
        response = client.get("/api/v1/users/me", headers=user["headers"])
        assert response.status_code == 200
        assert response.json() == user["user"] | {"updatedAt": response.json()["updatedAt"]}

    def test_update_profile(self, client, auth):
        response = client.patch(
            "/api/v1/users/me",
            headers=auth,
            json={"displayName": "Lefty", "handedness": "LEFT", "tuning": "D2,A2,D3,G3,B3,E4"},
        )
        assert response.status_code == 200
        body = response.json()
        assert (body["displayName"], body["handedness"], body["tuning"], body["timezone"]) == (
            "Lefty", "LEFT", "D2,A2,D3,G3,B3,E4", "UTC",
        )
        assert client.get("/api/v1/users/me", headers=auth).json()["handedness"] == "LEFT"

    @pytest.mark.parametrize("payload", [{"handedness": "BOTH"}, {"email": "x@example.com"}, {"displayName": "x" * 81}])
    def test_update_profile_validation(self, client, auth, payload):
        assert client.patch("/api/v1/users/me", headers=auth, json=payload).status_code == 422

    def test_deleted_user_token_returns_404(self, client, user, db):
        db.execute("DELETE FROM users WHERE id = ?", (user["user"]["id"],))
        db.commit()
        assert client.get("/api/v1/users/me", headers=user["headers"]).status_code == 404
