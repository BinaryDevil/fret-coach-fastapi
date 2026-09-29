from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Read a small .env file without adding a settings dependency."""
    path = Path.cwd() / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def setting(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


def database_path() -> Path:
    """Resolve the local SQLite URL used by this learning port."""
    url = setting("DATABASE_URL", "sqlite:///./fret_coach.db") or ""
    if not url.startswith("sqlite:///"):
        raise RuntimeError(
            "This learning port uses SQLite. Set DATABASE_URL=sqlite:///./fret_coach.db; "
            "the original PostgreSQL database is not modified or connected to."
        )
    raw_path = url.removeprefix("sqlite:///")
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


JWT_ACCESS_SECRET = setting(
    "JWT_ACCESS_SECRET", "local-development-access-secret-change-before-deploy"
) or ""
JWT_REFRESH_SECRET = setting(
    "JWT_REFRESH_SECRET", "local-development-refresh-secret-change-before-deploy"
) or ""
JWT_ACCESS_TTL = setting("JWT_ACCESS_TTL", "15m") or "15m"
JWT_REFRESH_TTL = setting("JWT_REFRESH_TTL", "30d") or "30d"
GOOGLE_CLIENT_ID = setting("GOOGLE_CLIENT_ID")
SUPABASE_URL = (setting("SUPABASE_URL") or "").rstrip("/")
SUPABASE_SECRET_KEY = setting("SUPABASE_SECRET_KEY") or setting(
    "SUPABASE_SERVICE_ROLE_KEY", ""
)
SUPABASE_SCORE_BUCKET = setting("SUPABASE_SCORE_BUCKET", "fret-coach-scores") or "fret-coach-scores"
