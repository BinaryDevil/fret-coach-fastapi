from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.security as security
import seed
from app.database import connect
from app.main import app
from app.storage import StorageUnavailable


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch) -> None:
    """Point every test at its own SQLite file and make password hashing cheap."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setattr(security, "PBKDF2_ROUNDS", 1_000)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db():
    connection = connect()
    try:
        yield connection
    finally:
        connection.close()


PARSEABLE_SEED_EXERCISES = [
    # The bend exercise currently fails timeline validation; see test_seed.py.
    exercise for exercise in seed.EXERCISES if "{b" not in exercise["body"]
]


@pytest.fixture
def seeded(client, monkeypatch) -> dict[str, dict]:
    """Seed the demo exercises and return their detail views keyed by slug."""
    monkeypatch.setattr(seed, "EXERCISES", PARSEABLE_SEED_EXERCISES)
    seed.main()
    exercises = client.get("/api/v1/exercises").json()
    return {exercise["slug"]: client.get(f"/api/v1/exercises/{exercise['id']}").json() for exercise in exercises}


class FakeStorage:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.removed: list[str] = []
        self.unavailable = False

    def _check(self) -> None:
        if self.unavailable:
            raise StorageUnavailable("Score file storage is not configured")

    def create_signed_upload_url(self, key: str, mime_type: str) -> dict[str, object]:
        self._check()
        return {
            "uploadUrl": f"https://storage.test/upload/{key}",
            "uploadMethod": "PUT",
            "uploadHeaders": {"Content-Type": mime_type},
            "expiresInSeconds": 600,
        }

    def create_signed_download_url(self, key: str) -> dict[str, object]:
        self._check()
        return {"sourceUrl": f"https://storage.test/download/{key}", "expiresInSeconds": 600}

    def download(self, key: str) -> bytes:
        self._check()
        if key not in self.files:
            raise StorageUnavailable("Supabase Storage request failed")
        return self.files[key]

    def remove(self, key: str) -> None:
        self._check()
        self.removed.append(key)
        self.files.pop(key, None)


@pytest.fixture
def storage(monkeypatch) -> FakeStorage:
    fake = FakeStorage()
    monkeypatch.setattr(main_module, "ScoreStorage", lambda: fake)
    return fake


ALPHA_TEX_SONG = b'\\title "Riff"\n\\tempo 100\n.\n:4 0.6 2.6 3.6 5.6 | 0.5 2.5 3.5 r | (0.4 2.3) 5.4 |'


@pytest.fixture
def create_import(client, storage) -> Callable[..., dict]:
    """Create a score import and optionally upload/complete it; returns the latest JSON view."""

    def _create(headers: dict, *, data: bytes | None = ALPHA_TEX_SONG, complete: bool = True,
                format: str = "ALPHA_TEX", file_name: str = "riff.alphatex", title: str = "Riff") -> dict:
        created = client.post(
            "/api/v1/score-imports",
            headers=headers,
            json={"title": title, "format": format, "fileName": file_name,
                  "mimeType": "text/plain", "sizeBytes": 100},
        )
        assert created.status_code == 200, created.text
        body = created.json()
        if data is not None:
            storage.files[body["scoreImport"]["storageKey"]] = data
        if not complete:
            return body
        completed = client.post(f"/api/v1/score-imports/{body['scoreImport']['id']}/complete", headers=headers)
        assert completed.status_code == 200, completed.text
        return completed.json()

    return _create


@pytest.fixture
def register(client) -> Callable[..., dict]:
    counter = iter(range(1_000))

    def _register(email: str | None = None, password: str = "correct-horse", **extra) -> dict:
        email = email or f"player{next(counter)}@example.com"
        response = client.post(
            "/api/v1/auth/register", json={"email": email, "password": password, **extra}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        body["headers"] = {"Authorization": f"Bearer {body['accessToken']}"}
        return body

    return _register


@pytest.fixture
def user(register) -> dict:
    return register()


@pytest.fixture
def auth(user) -> dict[str, str]:
    return user["headers"]
