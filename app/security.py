from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import (
    GOOGLE_CLIENT_ID,
    JWT_ACCESS_SECRET,
    JWT_ACCESS_TTL,
    JWT_REFRESH_SECRET,
    JWT_REFRESH_TTL,
)


PBKDF2_ROUNDS = 310_000


def hash_password(password: str) -> str:
    salt = base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("=")
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _ttl_seconds(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", value.lower())
    if not match:
        raise ValueError("JWT token lifetime must use seconds, minutes, hours, or days")
    amount = int(match.group(1))
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86_400}[match.group(2)]
    if amount < 1:
        raise ValueError("JWT token lifetime must be positive")
    return amount * multiplier


def encode_token(payload: dict[str, object], secret: str, ttl: str) -> str:
    now = int(time.time())
    body = {**payload, "iat": now, "exp": now + _ttl_seconds(ttl)}
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    data = _b64(json.dumps(body, separators=(",", ":")).encode())
    signed = f"{header}.{data}".encode()
    signature = _b64(hmac.new(secret.encode(), signed, hashlib.sha256).digest())
    return f"{header}.{data}.{signature}"


def decode_token(token: str, secret: str) -> dict[str, object]:
    try:
        header, body, signature = token.split(".")
        expected = _b64(hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        if json.loads(_unb64(header)).get("alg") != "HS256":
            raise ValueError("unsupported token algorithm")
        payload = json.loads(_unb64(body))
        if not isinstance(payload, dict) or int(payload.get("exp", 0)) <= int(time.time()):
            raise ValueError("expired token")
        if not isinstance(payload.get("sub"), str):
            raise ValueError("token subject is missing")
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("Invalid token") from error


def issue_tokens(user: dict[str, object]) -> dict[str, object]:
    payload = {"sub": user["id"], "email": user["email"]}
    access = encode_token(payload, JWT_ACCESS_SECRET, JWT_ACCESS_TTL)
    refresh = encode_token(payload, JWT_REFRESH_SECRET, JWT_REFRESH_TTL)
    return {"accessToken": access, "refreshToken": refresh, "refreshTokenHash": hash_password(refresh)}


def verify_google_credential(credential: str) -> dict[str, object]:
    """Ask Google's tokeninfo endpoint to verify the signed ID token and claims."""
    if not GOOGLE_CLIENT_ID:
        raise RuntimeError("Google sign-in is not configured")
    url = "https://oauth2.googleapis.com/tokeninfo?" + urlencode({"id_token": credential})
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ValueError("Invalid Google credential") from error
    if not isinstance(payload, dict):
        raise ValueError("Invalid Google credential")
    if payload.get("aud") != GOOGLE_CLIENT_ID:
        raise ValueError("Invalid Google credential audience")
    if payload.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
        raise ValueError("Invalid Google credential issuer")
    try:
        if int(payload.get("exp", 0)) <= int(time.time()):
            raise ValueError("Expired Google credential")
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid Google credential expiry") from error
    if payload.get("email_verified") not in {True, "true"}:
        raise ValueError("Google credential does not include a verified email")
    if not payload.get("sub") or not payload.get("email"):
        raise ValueError("Google credential does not include a verified email")
    return payload


def access_secret() -> str:
    return JWT_ACCESS_SECRET


def refresh_secret() -> str:
    return JWT_REFRESH_SECRET
