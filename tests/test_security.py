from __future__ import annotations

import io
import json
import time
from urllib.error import URLError

import pytest

import app.security as security
from app.security import (
    _b64,
    _ttl_seconds,
    decode_token,
    encode_token,
    hash_password,
    issue_tokens,
    verify_google_credential,
    verify_password,
)

SECRET = "s" * 40


class TestPasswords:
    def test_hash_round_trips(self):
        encoded = hash_password("correct-horse")
        assert encoded.startswith("pbkdf2_sha256$1000$")
        assert verify_password("correct-horse", encoded)
        assert not verify_password("wrong-horse", encoded)

    def test_hashes_are_salted(self):
        assert hash_password("same") != hash_password("same")

    def test_verify_uses_rounds_stored_in_hash(self, monkeypatch):
        encoded = hash_password("pw")
        monkeypatch.setattr(security, "PBKDF2_ROUNDS", 2_000)
        assert verify_password("pw", encoded)

    @pytest.mark.parametrize("encoded", ["", "garbage", "bcrypt$10$salt$digest", "pbkdf2_sha256$nan$salt$x"])
    def test_rejects_malformed_or_foreign_hashes(self, encoded):
        assert not verify_password("pw", encoded)


class TestTtl:
    @pytest.mark.parametrize(
        ("value", "seconds"),
        [("30s", 30), ("15m", 900), ("2h", 7200), ("30d", 2_592_000), (" 5M ", 300)],
    )
    def test_parses_units(self, value, seconds):
        assert _ttl_seconds(value) == seconds

    @pytest.mark.parametrize("value", ["15", "1w", "abc", "-5m", "0s"])
    def test_rejects_invalid(self, value):
        with pytest.raises(ValueError):
            _ttl_seconds(value)


class TestTokens:
    def test_round_trip(self):
        token = encode_token({"sub": "user-1", "email": "a@b.c"}, SECRET, "15m")
        payload = decode_token(token, SECRET)
        assert payload["sub"] == "user-1"
        assert payload["email"] == "a@b.c"
        assert payload["exp"] - payload["iat"] == 900

    def test_wrong_secret_rejected(self):
        token = encode_token({"sub": "u"}, SECRET, "15m")
        with pytest.raises(ValueError, match="Invalid token"):
            decode_token(token, "x" * 40)

    def test_tampered_payload_rejected(self):
        header, _, signature = encode_token({"sub": "u"}, SECRET, "15m").split(".")
        forged = _b64(json.dumps({"sub": "admin", "exp": int(time.time()) + 999}).encode())
        with pytest.raises(ValueError):
            decode_token(f"{header}.{forged}.{signature}", SECRET)

    def test_expired_rejected(self, monkeypatch):
        token = encode_token({"sub": "u"}, SECRET, "1s")
        real_time = time.time
        monkeypatch.setattr(security.time, "time", lambda: real_time() + 5)
        with pytest.raises(ValueError):
            decode_token(token, SECRET)

    def test_missing_subject_rejected(self):
        token = encode_token({"email": "a@b.c"}, SECRET, "15m")
        with pytest.raises(ValueError):
            decode_token(token, SECRET)

    def test_non_hs256_header_rejected(self):
        import hashlib
        import hmac

        header = _b64(json.dumps({"alg": "none"}).encode())
        body = _b64(json.dumps({"sub": "u", "exp": int(time.time()) + 60}).encode())
        signature = _b64(hmac.new(SECRET.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
        with pytest.raises(ValueError):
            decode_token(f"{header}.{body}.{signature}", SECRET)

    @pytest.mark.parametrize("token", ["", "a.b", "a.b.c.d", "not-a-token"])
    def test_malformed_rejected(self, token):
        with pytest.raises(ValueError):
            decode_token(token, SECRET)

    def test_issue_tokens_uses_separate_secrets(self):
        tokens = issue_tokens({"id": "user-1", "email": "a@b.c"})
        assert decode_token(tokens["accessToken"], security.JWT_ACCESS_SECRET)["sub"] == "user-1"
        assert decode_token(tokens["refreshToken"], security.JWT_REFRESH_SECRET)["sub"] == "user-1"
        with pytest.raises(ValueError):
            decode_token(tokens["accessToken"], security.JWT_REFRESH_SECRET)
        assert verify_password(tokens["refreshToken"], tokens["refreshTokenHash"])


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _google_claims(**overrides) -> dict:
    claims = {
        "aud": "client-123",
        "iss": "https://accounts.google.com",
        "exp": str(int(time.time()) + 3600),
        "email_verified": "true",
        "sub": "google-sub",
        "email": "g@example.com",
    }
    claims.update(overrides)
    return claims


class TestGoogleCredential:
    @pytest.fixture(autouse=True)
    def configured(self, monkeypatch):
        monkeypatch.setattr(security, "GOOGLE_CLIENT_ID", "client-123")

    def _serve(self, monkeypatch, payload):
        monkeypatch.setattr(
            security, "urlopen", lambda request, timeout: _Response(json.dumps(payload).encode())
        )

    def test_accepts_valid_claims(self, monkeypatch):
        self._serve(monkeypatch, _google_claims())
        assert verify_google_credential("token")["email"] == "g@example.com"

    def test_not_configured(self, monkeypatch):
        monkeypatch.setattr(security, "GOOGLE_CLIENT_ID", None)
        with pytest.raises(RuntimeError):
            verify_google_credential("token")

    @pytest.mark.parametrize(
        "overrides",
        [
            {"aud": "someone-else"},
            {"iss": "evil.example.com"},
            {"exp": str(int(time.time()) - 10)},
            {"exp": "soon"},
            {"email_verified": "false"},
            {"email": ""},
            {"sub": None},
        ],
    )
    def test_rejects_bad_claims(self, monkeypatch, overrides):
        self._serve(monkeypatch, _google_claims(**overrides))
        with pytest.raises(ValueError):
            verify_google_credential("token")

    def test_network_failure_is_invalid_credential(self, monkeypatch):
        def fail(request, timeout):
            raise URLError("offline")

        monkeypatch.setattr(security, "urlopen", fail)
        with pytest.raises(ValueError, match="Invalid Google credential"):
            verify_google_credential("token")
