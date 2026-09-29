from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import JWT_ACCESS_SECRET, JWT_REFRESH_SECRET
from app.database import get_db, initialize_database
from app.schemas import (
    AttemptCorrectionBody,
    Difficulty,
    ExerciseType,
    GoogleLoginBody,
    Handedness,
    LoginBody,
    PracticeAttemptCreateBody,
    PracticeNoteOutcome,
    PrivateHistoryQuery,
    RecordingCreateBody,
    RefreshBody,
    RegisterBody,
    ScoreFormat,
    ScoreImportCreateBody,
    SessionCreateBody,
    TelemetryBody,
    UpdateProfileBody,
)
from app.score_timeline import (
    ScoreParseError,
    generate_from_bytes,
    score_revision,
)
from app.security import (
    decode_token,
    hash_password,
    issue_tokens,
    refresh_secret,
    verify_google_credential,
    verify_password,
)
from app.storage import ScoreStorage, StorageUnavailable


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _id() -> str:
    return str(uuid4())


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _load_json(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _camel(key: str) -> str:
    first, *rest = key.split("_")
    return first + "".join(part.capitalize() for part in rest)


def _output(value: object) -> object:
    """Convert database rows into the source API's camelCase JSON shape."""
    if isinstance(value, sqlite3.Row):
        value = dict(value)
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            if key in {"configuration", "timeline", "metadata", "expected_pitches", "bend", "tuning"}:
                item = _load_json(item)
            if key in {"is_published", "is_helpful"} and item is not None:
                item = bool(item)
            result[_camel(key)] = _output(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_output(item) for item in value]
    return value


def _profile(user: dict[str, object] | sqlite3.Row) -> dict[str, object]:
    row = dict(user)
    return {
        "id": row["id"],
        "email": row["email"],
        "displayName": row.get("display_name"),
        "handedness": row.get("handedness"),
        "tuning": row.get("tuning"),
        "timezone": row.get("timezone"),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
    }


def _token_response(db: sqlite3.Connection, user: dict[str, object] | sqlite3.Row) -> dict[str, object]:
    row = dict(user)
    tokens = issue_tokens({"id": row["id"], "email": row["email"]})
    updated_at = _iso()
    db.execute(
        "UPDATE users SET refresh_token_hash = ?, updated_at = ? WHERE id = ?",
        (tokens["refreshTokenHash"], updated_at, row["id"]),
    )
    row["updated_at"] = updated_at
    return {
        "accessToken": tokens["accessToken"],
        "refreshToken": tokens["refreshToken"],
        "user": _profile(row),
    }


def _http(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


bearer = HTTPBearer(auto_error=False)


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict[str, str]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer access token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_token(credentials.credentials, JWT_ACCESS_SECRET)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    return {"sub": str(payload["sub"]), "email": str(payload.get("email", ""))}


@asynccontextmanager
async def lifespan(_: FastAPI):
    if len(JWT_ACCESS_SECRET) < 32 or len(JWT_REFRESH_SECRET) < 32:
        raise RuntimeError("JWT access and refresh secrets must each contain at least 32 characters")
    if JWT_ACCESS_SECRET == JWT_REFRESH_SECRET:
        raise RuntimeError("JWT access and refresh secrets must be different")
    initialize_database()
    yield


app = FastAPI(
    title="Fret Coach API",
    description="Learning-focused FastAPI port of the Fret Coach backend.",
    version="v1",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/api/v1/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok", "timestamp": _iso()}


# Authentication ------------------------------------------------------------


@app.post("/api/v1/auth/register", tags=["auth"])
def register(body: RegisterBody, db: sqlite3.Connection = Depends(get_db)):
    email = str(body.email).lower()
    if db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
        raise _http(409, "Email is already registered")
    user_id, now = _id(), _iso()
    try:
        db.execute(
            """INSERT INTO users (id, email, password_hash, display_name, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, email, hash_password(body.password), body.display_name, now, now),
        )
        db.execute(
            "INSERT INTO user_progress (id, user_id, updated_at) VALUES (?, ?, ?)",
            (_id(), user_id, now),
        )
    except sqlite3.IntegrityError as error:
        raise _http(409, "Email is already registered") from error
    return _token_response(db, db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


@app.post("/api/v1/auth/login", tags=["auth"], status_code=status.HTTP_200_OK)
def login(body: LoginBody, db: sqlite3.Connection = Depends(get_db)):
    user = db.execute("SELECT * FROM users WHERE email = ?", (str(body.email).lower(),)).fetchone()
    if not user or not verify_password(body.password, user["password_hash"]):
        raise _http(401, "Invalid email or password")
    return _token_response(db, user)


@app.post("/api/v1/auth/google", tags=["auth"], status_code=status.HTTP_200_OK)
def google_login(body: GoogleLoginBody, db: sqlite3.Connection = Depends(get_db)):
    try:
        claims = verify_google_credential(body.credential)
    except RuntimeError as error:
        raise _http(503, "Google sign-in is not configured") from error
    except ValueError as error:
        raise _http(401, str(error)) from error
    email = str(claims["email"]).lower()
    subject = str(claims["sub"])
    user = db.execute("SELECT * FROM users WHERE google_subject = ?", (subject,)).fetchone()
    if user is None:
        existing = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if existing and existing["google_subject"] and existing["google_subject"] != subject:
            raise _http(409, "This email is already linked to another Google account")
        now = _iso()
        if existing:
            display_name = existing["display_name"] or claims.get("name")
            db.execute(
                "UPDATE users SET google_subject = ?, display_name = ?, updated_at = ? WHERE id = ?",
                (subject, display_name, now, existing["id"]),
            )
            user = db.execute("SELECT * FROM users WHERE id = ?", (existing["id"],)).fetchone()
        else:
            user_id = _id()
            db.execute(
                """INSERT INTO users (id, email, password_hash, google_subject, display_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, email, hash_password(_id()), subject, claims.get("name"), now, now),
            )
            db.execute(
                "INSERT INTO user_progress (id, user_id, updated_at) VALUES (?, ?, ?)",
                (_id(), user_id, now),
            )
            user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _token_response(db, user)


@app.post("/api/v1/auth/refresh", tags=["auth"], status_code=status.HTTP_200_OK)
def refresh(body: RefreshBody, db: sqlite3.Connection = Depends(get_db)):
    try:
        payload = decode_token(body.refresh_token, refresh_secret())
    except ValueError as error:
        raise _http(401, "Invalid refresh token") from error
    user = db.execute("SELECT * FROM users WHERE id = ?", (payload["sub"],)).fetchone()
    if not user or not user["refresh_token_hash"] or not verify_password(
        body.refresh_token, user["refresh_token_hash"]
    ):
        raise _http(401, "Invalid refresh token")
    return _token_response(db, user)


@app.post("/api/v1/auth/logout", tags=["auth"], status_code=status.HTTP_204_NO_CONTENT)
def logout(user: dict[str, str] = Depends(current_user), db: sqlite3.Connection = Depends(get_db)):
    db.execute(
        "UPDATE users SET refresh_token_hash = NULL, updated_at = ? WHERE id = ?",
        (_iso(), user["sub"]),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# User profile --------------------------------------------------------------


@app.get("/api/v1/users/me", tags=["users"])
def get_my_profile(user: dict[str, str] = Depends(current_user), db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM users WHERE id = ?", (user["sub"],)).fetchone()
    if row is None:
        raise _http(404, "User not found")
    return _profile(row)


@app.patch("/api/v1/users/me", tags=["users"])
def update_my_profile(
    body: UpdateProfileBody,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    changes = body.model_dump(exclude_unset=True)
    aliases = {"display_name": "display_name", "handedness": "handedness", "tuning": "tuning", "timezone": "timezone"}
    for key, value in changes.items():
        if key == "handedness" and isinstance(value, Handedness):
            value = value.value
        db.execute(
            f"UPDATE users SET {aliases[key]} = ?, updated_at = ? WHERE id = ?",
            (value, _iso(), user["sub"]),
        )
    row = db.execute("SELECT * FROM users WHERE id = ?", (user["sub"],)).fetchone()
    if row is None:
        raise _http(404, "User not found")
    return _profile(row)


# Exercise library ----------------------------------------------------------


def _exercise_card(row: sqlite3.Row, *, include_details: bool = True) -> dict[str, object]:
    fields = [
        "id", "slug", "type", "difficulty", "title", "description", "instructions",
        "skill_focus", "estimated_minutes", "library_order", "configuration", "reference_audio_url",
        "created_at", "updated_at",
    ]
    if not include_details:
        fields = ["id", "slug", "type", "difficulty", "title", "skill_focus", "estimated_minutes", "configuration"]
    return _output({key: row[key] for key in fields})


def _exercise_loop_regions(db: sqlite3.Connection, exercise_id: str) -> list[dict[str, object]]:
    return [
        _output(row)
        for row in db.execute(
            """SELECT id, title, start_measure, end_measure, default_bpm FROM loop_regions
               WHERE exercise_id = ? ORDER BY start_measure ASC""",
            (exercise_id,),
        ).fetchall()
    ]


def _exercise_detail(db: sqlite3.Connection, exercise: sqlite3.Row) -> dict[str, object]:
    response = _exercise_card(exercise)
    score = db.execute("SELECT * FROM exercise_scores WHERE exercise_id = ?", (exercise["id"],)).fetchone()
    if score is None:
        response["score"] = None
    else:
        score_view = _output({
            key: score[key]
            for key in (
                "format", "file_name", "storage_key", "content", "time_signature", "default_bpm",
                "total_measures", "backing_track_url", "backing_track_offset_ms", "event_version",
                "event_source_hash", "timeline",
            )
        })
        score_view["revision"] = score_revision(score["event_version"], score["event_source_hash"])
        response["score"] = score_view
    response["loopRegions"] = _exercise_loop_regions(db, exercise["id"])
    return response


@app.get("/api/v1/exercises", tags=["exercises"])
def list_exercises(
    type: ExerciseType | None = None,
    difficulty: Difficulty | None = None,
    db: sqlite3.Connection = Depends(get_db),
):
    sql = "SELECT * FROM exercises WHERE is_published = 1"
    params: list[object] = []
    if type is not None:
        sql += " AND type = ?"
        params.append(type.value)
    if difficulty is not None:
        sql += " AND difficulty = ?"
        params.append(difficulty.value)
    sql += " ORDER BY library_order ASC"
    items = []
    for exercise in db.execute(sql, params).fetchall():
        card = _exercise_card(exercise)
        score = db.execute("SELECT * FROM exercise_scores WHERE exercise_id = ?", (exercise["id"],)).fetchone()
        if score is None:
            card["score"] = None
        else:
            card["score"] = _output({
                key: score[key] for key in (
                    "format", "file_name", "storage_key", "time_signature", "default_bpm",
                    "total_measures", "backing_track_url", "backing_track_offset_ms", "event_version", "event_source_hash"
                )
            })
        card["loopRegions"] = _exercise_loop_regions(db, exercise["id"])
        items.append(card)
    return items


def _get_published_exercise(db: sqlite3.Connection, *, exercise_id: str | None = None, slug: str | None = None):
    key, value = ("id", exercise_id) if exercise_id is not None else ("slug", slug)
    row = db.execute(f"SELECT * FROM exercises WHERE {key} = ? AND is_published = 1", (value,)).fetchone()
    if row is None:
        raise _http(404, "Exercise not found")
    return row


@app.get("/api/v1/exercises/slug/{slug}", tags=["exercises"])
def get_exercise_by_slug(slug: str, db: sqlite3.Connection = Depends(get_db)):
    return _exercise_detail(db, _get_published_exercise(db, slug=slug))


@app.get("/api/v1/exercises/{exercise_id}/progress", tags=["exercises"])
def get_exercise_progress(
    exercise_id: str,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    _get_published_exercise(db, exercise_id=exercise_id)
    progress = db.execute(
        "SELECT * FROM user_exercise_progress WHERE user_id = ? AND exercise_id = ?",
        (user["sub"], exercise_id),
    ).fetchone()
    return _output(progress) if progress else None


@app.get("/api/v1/exercises/{exercise_id}", tags=["exercises"])
def get_exercise(exercise_id: str, db: sqlite3.Connection = Depends(get_db)):
    return _exercise_detail(db, _get_published_exercise(db, exercise_id=exercise_id))


# Dashboard and progress ----------------------------------------------------


def _target_bpm(configuration: object) -> int | None:
    config = _load_json(configuration)
    if isinstance(config, dict) and isinstance(config.get("targetBpm"), (int, float)):
        return int(config["targetBpm"])
    return None


def _coach_note(accuracy: int | None, bpm: int | None) -> str:
    if accuracy is None:
        return "Start with a clean, controlled pass and raise the tempo only when it feels relaxed."
    if accuracy >= 90 and bpm:
        return f"Strong last pass. Try {bpm + 4} BPM when your timing still feels even."
    if accuracy >= 75:
        return "Your accuracy is building. Repeat at the same BPM and aim for cleaner note changes."
    return "Slow the tempo slightly and prioritize even timing before adding speed."


@app.get("/api/v1/dashboard/me", tags=["dashboard"])
def dashboard(user: dict[str, str] = Depends(current_user), db: sqlite3.Connection = Depends(get_db)):
    user_id = user["sub"]
    progress = db.execute("SELECT * FROM user_progress WHERE user_id = ?", (user_id,)).fetchone()
    today = _now().date()
    week_start = date(today.year, today.month, today.day) - timedelta(days=today.weekday())
    aggregate = db.execute(
        """SELECT AVG(accuracy_percent) AS average_accuracy, SUM(active_duration_ms) AS active_ms
           FROM practice_attempts WHERE user_id = ? AND completed_at >= ?""",
        (user_id, datetime.combine(week_start, time.min, timezone.utc).isoformat().replace("+00:00", "Z")),
    ).fetchone()
    latest = db.execute(
        "SELECT * FROM practice_attempts WHERE user_id = ? ORDER BY completed_at DESC, id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    recommendations = db.execute(
        "SELECT * FROM exercises WHERE is_published = 1 ORDER BY library_order ASC LIMIT 3"
    ).fetchall()
    recommendation_cards = []
    for index, exercise in enumerate(recommendations, start=1):
        card = _exercise_card(exercise, include_details=False)
        card["rank"] = index
        card["targetBpm"] = _target_bpm(exercise["configuration"])
        recommendation_cards.append(card)
    next_practice: dict[str, object] | None = None
    latest_accuracy: int | None = None
    if latest is not None:
        latest_accuracy = latest["accuracy_percent"]
        exercise = db.execute("SELECT * FROM exercises WHERE id = ?", (latest["exercise_id"],)).fetchone() if latest["exercise_id"] else None
        next_practice = _exercise_card(exercise, include_details=False) if exercise else {}
        next_practice["targetBpm"] = latest["bpm"]
    elif recommendation_cards:
        first = recommendations[0]
        next_practice = _exercise_card(first, include_details=False)
        next_practice["targetBpm"] = _target_bpm(first["configuration"])
    return {
        "nextPractice": next_practice,
        "stats": {
            "currentDayStreak": progress["current_day_streak"] if progress else 0,
            "averageAccuracyPercent": round(aggregate["average_accuracy"] or 0),
            "playedThisWeekMs": aggregate["active_ms"] or 0,
        },
        "coachNote": _coach_note(latest_accuracy, next_practice.get("targetBpm") if next_practice else None),
        "recommendations": recommendation_cards,
    }


def _utc_day(value: datetime) -> datetime:
    value = value.astimezone(timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _record_session_progress(db: sqlite3.Connection, user_id: str, practice_seconds: int) -> None:
    current = db.execute("SELECT * FROM user_progress WHERE user_id = ?", (user_id,)).fetchone()
    now = _now()
    today = _utc_day(now)
    if current is None:
        current_streak, longest = 0, 0
        current_id = _id()
        db.execute(
            "INSERT INTO user_progress (id, user_id, updated_at) VALUES (?, ?, ?)",
            (current_id, user_id, _iso(now)),
        )
        current = db.execute("SELECT * FROM user_progress WHERE id = ?", (current_id,)).fetchone()
    else:
        current_streak, longest = current["current_day_streak"], current["longest_day_streak"]
    previous = _parse_date(current["last_practiced_on"]) if current["last_practiced_on"] else None
    yesterday = today - timedelta(days=1)
    if previous and _utc_day(previous) == today:
        streak = current_streak
    elif previous and _utc_day(previous) == yesterday:
        streak = current_streak + 1
    else:
        streak = 1
    db.execute(
        """UPDATE user_progress SET total_sessions = total_sessions + 1,
           total_practice_seconds = total_practice_seconds + ?, current_day_streak = ?,
           longest_day_streak = ?, last_practiced_on = ?, updated_at = ? WHERE user_id = ?""",
        (practice_seconds, streak, max(longest, streak), _iso(today), _iso(now), user_id),
    )


@app.get("/api/v1/progress/me", tags=["progress"])
def progress_summary(user: dict[str, str] = Depends(current_user), db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM user_progress WHERE user_id = ?", (user["sub"],)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO user_progress (id, user_id, updated_at) VALUES (?, ?, ?)",
            (_id(), user["sub"], _iso()),
        )
        row = db.execute("SELECT * FROM user_progress WHERE user_id = ?", (user["sub"],)).fetchone()
    return _output(row)


# Practice sessions and recording analysis ---------------------------------


@app.post("/api/v1/practice-sessions", tags=["practice-sessions"])
def create_practice_session(
    body: SessionCreateBody,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    exercise = _get_published_exercise(db, exercise_id=body.exercise_id)
    score = db.execute("SELECT * FROM exercise_scores WHERE exercise_id = ?", (exercise["id"],)).fetchone()
    if score is None:
        raise _http(404, "Playable exercise not found")
    loop = None
    if body.loop_region_id:
        loop = db.execute(
            "SELECT * FROM loop_regions WHERE id = ? AND exercise_id = ?",
            (body.loop_region_id, body.exercise_id),
        ).fetchone()
        if loop is None:
            raise _http(400, "Loop region does not belong to exercise")
    now = _iso()
    session_id = _id()
    bpm = body.target_bpm or (loop["default_bpm"] if loop else None) or score["default_bpm"]
    db.execute(
        """INSERT INTO practice_sessions (id, user_id, exercise_id, loop_region_id, target_bpm,
           status, started_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'IN_PROGRESS', ?, ?, ?)""",
        (session_id, user["sub"], exercise["id"], loop["id"] if loop else None, bpm, now, now, now),
    )
    session = db.execute("SELECT * FROM practice_sessions WHERE id = ?", (session_id,)).fetchone()
    response = dict(_output(session))
    response["exercise"] = _exercise_detail(db, exercise)
    response["loopRegion"] = _output(loop) if loop else None
    return response


@app.post("/api/v1/practice-sessions/{session_id}/recordings", tags=["practice-sessions"])
def add_recording(
    session_id: str,
    body: RecordingCreateBody,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    session = db.execute(
        "SELECT id FROM practice_sessions WHERE id = ? AND user_id = ? AND status = 'IN_PROGRESS'",
        (session_id, user["sub"]),
    ).fetchone()
    if session is None:
        raise _http(404, "Active practice session not found")
    now = _iso()
    recording_id = _id()
    db.execute(
        """INSERT INTO performance_recordings (id, session_id, storage_key, mime_type, duration_ms, recorded_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (recording_id, session_id, body.storage_key, body.mime_type, body.duration_ms, now, now),
    )
    return _output(db.execute("SELECT * FROM performance_recordings WHERE id = ?", (recording_id,)).fetchone())


@app.post("/api/v1/practice-sessions/{session_id}/complete", tags=["practice-sessions"])
def complete_practice_session(
    session_id: str,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    session = db.execute(
        "SELECT * FROM practice_sessions WHERE id = ? AND user_id = ? AND status = 'IN_PROGRESS'",
        (session_id, user["sub"]),
    ).fetchone()
    if session is None:
        raise _http(404, "Active practice session not found")
    completed_at = _now()
    duration = max(0, math.floor((completed_at - _parse_date(session["started_at"])).total_seconds()))
    db.execute(
        """UPDATE practice_sessions SET status = 'COMPLETED', completed_at = ?, duration_seconds = ?, updated_at = ?
           WHERE id = ?""",
        (_iso(completed_at), duration, _iso(completed_at), session_id),
    )
    _record_session_progress(db, user["sub"], duration)
    return _output(db.execute("SELECT * FROM practice_sessions WHERE id = ?", (session_id,)).fetchone())


def _analysis_response(db: sqlite3.Connection, recording_id: str, *, with_feedback: bool) -> dict[str, object]:
    row = db.execute("SELECT * FROM performance_analyses WHERE recording_id = ?", (recording_id,)).fetchone()
    if row is None:
        return {"status": "NOT_REQUESTED"}
    response = dict(_output(row))
    if with_feedback:
        response["feedback"] = [
            _output(feedback)
            for feedback in db.execute(
                "SELECT * FROM performance_feedback WHERE analysis_id = ? ORDER BY start_ms ASC",
                (row["id"],),
            ).fetchall()
        ]
    return response


@app.post("/api/v1/recordings/{recording_id}/analysis", tags=["analysis"])
def queue_analysis(
    recording_id: str,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    recording = db.execute(
        """SELECT r.id FROM performance_recordings r JOIN practice_sessions s ON s.id = r.session_id
           WHERE r.id = ? AND s.user_id = ?""",
        (recording_id, user["sub"]),
    ).fetchone()
    if recording is None:
        raise _http(404, "Recording not found")
    current = db.execute("SELECT * FROM performance_analyses WHERE recording_id = ?", (recording_id,)).fetchone()
    if current is None:
        now, analysis_id = _iso(), _id()
        db.execute(
            "INSERT INTO performance_analyses (id, recording_id, status, created_at, updated_at) VALUES (?, ?, 'QUEUED', ?, ?)",
            (analysis_id, recording_id, now, now),
        )
        current = db.execute("SELECT * FROM performance_analyses WHERE id = ?", (analysis_id,)).fetchone()
    return _output(current)


@app.get("/api/v1/recordings/{recording_id}/analysis", tags=["analysis"])
def get_analysis(
    recording_id: str,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    recording = db.execute(
        """SELECT r.id FROM performance_recordings r JOIN practice_sessions s ON s.id = r.session_id
           WHERE r.id = ? AND s.user_id = ?""",
        (recording_id, user["sub"]),
    ).fetchone()
    if recording is None:
        raise _http(404, "Recording not found")
    return _analysis_response(db, recording_id, with_feedback=True)


# Practice attempts ---------------------------------------------------------


def _read_timeline(value: object) -> dict[str, object]:
    timeline = _load_json(value)
    if not isinstance(timeline, dict) or not isinstance(timeline.get("events"), list) or timeline.get("version", 0) < 1:
        raise _http(400, "Exercise score has no valid generated timeline")
    return timeline


def _target_events(
    timeline: dict[str, object],
    *,
    start_measure: int | None = None,
    end_measure: int | None = None,
    track_id: str | None = None,
) -> list[dict[str, object]]:
    targets = []
    for event_index, event in enumerate(timeline["events"]):
        if not isinstance(event, dict):
            continue
        pitches = event.get("pitches")
        if event.get("isRest") or not isinstance(pitches, list) or not pitches:
            continue
        if start_measure is not None and event.get("measure", 0) < start_measure:
            continue
        if end_measure is not None and event.get("measure", 0) > end_measure:
            continue
        if track_id is not None and event.get("trackId") != track_id:
            continue
        targets.append({**event, "eventIndex": event_index})
    return targets


def _attempt_with_results(db: sqlite3.Connection, attempt: sqlite3.Row) -> dict[str, object]:
    response = dict(_output(attempt))
    results = db.execute(
        "SELECT * FROM practice_note_results WHERE attempt_id = ? ORDER BY event_index ASC",
        (attempt["id"],),
    ).fetchall()
    response["noteResults"] = [_output(row) for row in results]
    return response


def _existing_attempt(db: sqlite3.Connection, client_run_id: str, user_id: str):
    attempt = db.execute(
        "SELECT * FROM practice_attempts WHERE client_run_id = ?", (client_run_id,)
    ).fetchone()
    if attempt is None:
        return None
    if attempt["user_id"] != user_id:
        raise _http(409, "clientRunId has already been used")
    return _attempt_with_results(db, attempt)


def _assert_bpm_range(bpm: int, configuration: object) -> None:
    config = _load_json(configuration)
    bpm_range = config.get("bpmRange") if isinstance(config, dict) else None
    if (
        not isinstance(bpm_range, list)
        or len(bpm_range) != 2
        or not all(isinstance(item, (int, float)) for item in bpm_range)
        or bpm < bpm_range[0]
        or bpm > bpm_range[1]
    ):
        raise _http(400, "BPM is outside the exercise range")


def _persisted_note_result(target: dict[str, object], source=None, *, outcome: str | None = None):
    value = {
        "timeline_event_id": target["id"],
        "event_index": target["eventIndex"],
        "measure": target["measure"],
        "beat": target["beat"],
        "start_tick": target["startTick"],
        "expected_pitches": _json(target["pitches"]),
        "outcome": outcome or source.outcome.value,
    }
    if source is not None:
        value.update({
            "detected_midi": source.detected_midi,
            "corrected_midi": source.corrected_midi,
            "cents_offset": source.cents_offset,
            "octave_shift": source.octave_shift,
            "confidence": source.confidence,
            "timing_offset_ms": source.timing_offset_ms,
            "bend": _json(source.bend.model_dump()) if source.bend else None,
        })
    return value


def _update_exercise_progress(
    db: sqlite3.Connection,
    *,
    user_id: str,
    exercise_id: str,
    duration_ms: int,
    accuracy: int,
    bpm: int,
    practiced_at: str,
) -> None:
    current = db.execute(
        "SELECT * FROM user_exercise_progress WHERE user_id = ? AND exercise_id = ?",
        (user_id, exercise_id),
    ).fetchone()
    if current is None:
        db.execute(
            """INSERT INTO user_exercise_progress (id, user_id, exercise_id, completed_passes,
               total_active_ms, best_accuracy_percent, best_bpm, latest_accuracy_percent,
               last_practiced_at, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
            (_id(), user_id, exercise_id, duration_ms, accuracy, bpm if accuracy >= 90 else 0,
             accuracy, practiced_at, practiced_at, practiced_at),
        )
    else:
        db.execute(
            """UPDATE user_exercise_progress SET completed_passes = completed_passes + 1,
               total_active_ms = total_active_ms + ?, best_accuracy_percent = ?, best_bpm = ?,
               latest_accuracy_percent = ?, last_practiced_at = ?, updated_at = ? WHERE id = ?""",
            (duration_ms, max(current["best_accuracy_percent"], accuracy),
             max(current["best_bpm"], bpm) if accuracy >= 90 else current["best_bpm"],
             accuracy, practiced_at, practiced_at, current["id"]),
        )


@app.post("/api/v1/practice-attempts", tags=["practice-attempts"])
def create_practice_attempt(
    body: PracticeAttemptCreateBody,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    client_run_id = str(body.client_run_id)
    existing = _existing_attempt(db, client_run_id, user["sub"])
    if existing is not None:
        return existing

    is_private = body.score_import_id is not None
    if is_private == (body.exercise_id is not None):
        raise _http(400, "An attempt must target either an exercise or a private arrangement")
    exercise_id = score_import_id = loop_region_id = track_id = None
    passage_start = passage_end = None

    if is_private:
        if (
            not body.track_id
            or body.passage_start_measure is None
            or body.passage_end_measure is None
            or body.passage_start_measure > body.passage_end_measure
            or body.loop_region_id
        ):
            raise _http(400, "A private attempt requires a valid track and measure range")
        imported = db.execute(
            """SELECT id, event_version, source_hash, timeline FROM score_imports
               WHERE id = ? AND user_id = ? AND status = 'READY'""",
            (body.score_import_id, user["sub"]),
        ).fetchone()
        if imported is None:
            raise _http(404, "Private arrangement not found")
        current_revision = score_revision(imported["event_version"], imported["source_hash"])
        if body.score_revision != current_revision:
            raise _http(409, "Score revision is stale; reload the arrangement before saving")
        timeline = _read_timeline(imported["timeline"])
        tracks = timeline.get("tracks", [])
        if not any(isinstance(track, dict) and track.get("id") == body.track_id for track in tracks):
            raise _http(400, "The selected track does not belong to the arrangement")
        if body.passage_end_measure > timeline.get("totalMeasures", 0):
            raise _http(400, "The selected passage is outside the arrangement")
        score_import_id = imported["id"]
        track_id = body.track_id
        passage_start, passage_end = body.passage_start_measure, body.passage_end_measure
        targets = _target_events(
            timeline,
            start_measure=passage_start,
            end_measure=passage_end,
            track_id=track_id,
        )
    else:
        if body.track_id or body.passage_start_measure or body.passage_end_measure:
            raise _http(400, "Private passage fields are not valid for an exercise attempt")
        exercise = db.execute(
            "SELECT * FROM exercises WHERE id = ? AND is_published = 1", (body.exercise_id,)
        ).fetchone()
        score = db.execute("SELECT * FROM exercise_scores WHERE exercise_id = ?", (body.exercise_id,)).fetchone()
        if exercise is None or score is None:
            raise _http(404, "Playable exercise not found")
        current_revision = score_revision(score["event_version"], score["event_source_hash"])
        if body.score_revision != current_revision:
            raise _http(409, "Score revision is stale; reload the exercise before saving")
        timeline = _read_timeline(score["timeline"])
        loop = None
        if body.loop_region_id:
            loop = db.execute(
                "SELECT * FROM loop_regions WHERE id = ? AND exercise_id = ?",
                (body.loop_region_id, exercise["id"]),
            ).fetchone()
            if loop is None:
                raise _http(400, "Loop region does not belong to exercise")
        _assert_bpm_range(body.bpm, exercise["configuration"])
        exercise_id = exercise["id"]
        loop_region_id = loop["id"] if loop else None
        targets = _target_events(
            timeline,
            start_measure=loop["start_measure"] if loop else None,
            end_measure=loop["end_measure"] if loop else None,
        )
    if not targets:
        raise _http(400, "The selected score range has no scoreable note events")

    targets_by_id = {target["id"]: target for target in targets}
    seen: set[str] = set()
    results: list[dict[str, object]] = []
    for submitted in body.note_results:
        target = targets_by_id.get(submitted.timeline_event_id)
        if target is None:
            raise _http(400, "A note result does not belong to the selected score range")
        if target["id"] in seen:
            raise _http(400, "A timeline event may only have one note result")
        if submitted.event_index != target["eventIndex"]:
            raise _http(400, "A note result has an invalid timeline event index")
        seen.add(target["id"])
        results.append(_persisted_note_result(target, submitted))
    for target in targets:
        if target["id"] not in seen:
            results.append(_persisted_note_result(target, outcome=PracticeNoteOutcome.MISSING.value))
    results.sort(key=lambda item: item["event_index"])

    correct = wrong = missing = extra = 0
    for item in results:
        outcome = item["outcome"]
        correct += outcome == PracticeNoteOutcome.CORRECT.value
        wrong += outcome == PracticeNoteOutcome.WRONG_PITCH.value
        missing += outcome == PracticeNoteOutcome.MISSING.value
        if outcome == PracticeNoteOutcome.EXTRA_NOTE.value:
            extra += 1
            missing += 1
    accuracy = math.floor(correct / len(targets) * 100 + 0.5)
    completed_at = _iso()
    attempt_id = _id()
    data = (
        attempt_id, client_run_id, user["sub"], exercise_id, score_import_id, loop_region_id,
        track_id, passage_start, passage_end, current_revision, body.bpm, body.count_in_beats,
        body.active_duration_ms, len(targets), int(correct), int(wrong), int(missing), int(extra),
        accuracy, _iso(body.started_at) if body.started_at else None, completed_at, completed_at, completed_at,
    )
    try:
        db.execute(
            """INSERT INTO practice_attempts (id, client_run_id, user_id, exercise_id, score_import_id,
               loop_region_id, track_id, passage_start_measure, passage_end_measure, score_revision, bpm,
               count_in_beats, active_duration_ms, total_target_events, correct_count, wrong_pitch_count,
               missing_count, extra_note_count, accuracy_percent, started_at, completed_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            data,
        )
        for result in results:
            result_id = _id()
            db.execute(
                """INSERT INTO practice_note_results (id, attempt_id, timeline_event_id, event_index, measure,
                   beat, start_tick, expected_pitches, outcome, detected_midi, corrected_midi, cents_offset,
                   octave_shift, confidence, timing_offset_ms, bend, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (result_id, attempt_id, result["timeline_event_id"], result["event_index"], result["measure"],
                 result["beat"], result["start_tick"], result["expected_pitches"], result["outcome"],
                 result.get("detected_midi"), result.get("corrected_midi"), result.get("cents_offset"),
                 result.get("octave_shift"), result.get("confidence"), result.get("timing_offset_ms"),
                 result.get("bend"), completed_at),
            )
        if exercise_id:
            _update_exercise_progress(
                db, user_id=user["sub"], exercise_id=exercise_id,
                duration_ms=body.active_duration_ms, accuracy=accuracy, bpm=body.bpm,
                practiced_at=completed_at,
            )
        db.commit()
    except sqlite3.IntegrityError as error:
        db.rollback()
        retry = _existing_attempt(db, client_run_id, user["sub"])
        if retry is not None:
            return retry
        raise _http(409, "Practice attempt could not be saved") from error
    attempt = db.execute("SELECT * FROM practice_attempts WHERE id = ?", (attempt_id,)).fetchone()
    return _attempt_with_results(db, attempt)


@app.get("/api/v1/practice-attempts", tags=["practice-attempts"])
def list_practice_attempts(
    exercise_id: str | None = Query(default=None, alias="exerciseId"),
    score_import_id: str | None = Query(default=None, alias="scoreImportId"),
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    where, params = ["user_id = ?"], [user["sub"]]
    if exercise_id:
        where.append("exercise_id = ?")
        params.append(exercise_id)
    if score_import_id:
        where.append("score_import_id = ?")
        params.append(score_import_id)
    if cursor:
        cursor_row = db.execute(
            "SELECT id, completed_at FROM practice_attempts WHERE id = ? AND user_id = ?",
            (cursor, user["sub"]),
        ).fetchone()
        if cursor_row is None:
            raise _http(400, "Invalid practice attempt cursor")
        where.append("(completed_at < ? OR (completed_at = ? AND id < ?))")
        params.extend([cursor_row["completed_at"], cursor_row["completed_at"], cursor_row["id"]])
    columns = "id, exercise_id, score_import_id, loop_region_id, track_id, passage_start_measure, passage_end_measure, score_revision, status, bpm, active_duration_ms, total_target_events, correct_count, wrong_pitch_count, missing_count, extra_note_count, accuracy_percent, completed_at"
    rows = db.execute(
        f"SELECT {columns} FROM practice_attempts WHERE {' AND '.join(where)} ORDER BY completed_at DESC, id DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    items = rows[:limit]
    return {"items": [_output(row) for row in items], "nextCursor": items[-1]["id"] if has_more and items else None}


@app.get("/api/v1/practice-attempts/private/{score_import_id}/history", tags=["practice-attempts"])
def private_attempt_history(
    score_import_id: str,
    query: PrivateHistoryQuery = Depends(),
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    imported = db.execute(
        "SELECT id FROM score_imports WHERE id = ? AND user_id = ?",
        (score_import_id, user["sub"]),
    ).fetchone()
    if imported is None:
        raise _http(404, "Private arrangement not found")
    rows = db.execute(
        """SELECT * FROM practice_attempts WHERE user_id = ? AND score_import_id = ? AND track_id = ?
           AND passage_start_measure = ? AND passage_end_measure = ? ORDER BY completed_at ASC, id ASC""",
        (user["sub"], score_import_id, query.track_id, query.passage_start_measure, query.passage_end_measure),
    ).fetchall()
    items = []
    for attempt in rows:
        item = _attempt_with_results(db, attempt)
        corrections = db.execute(
            "SELECT * FROM attempt_corrections WHERE attempt_id = ? ORDER BY created_at ASC",
            (attempt["id"],),
        ).fetchall()
        item["corrections"] = [_output(row) for row in corrections]
        items.append(item)
    return {"baselineAttemptId": items[0]["id"] if items else None, "items": items}


@app.get("/api/v1/practice-attempts/{attempt_id}", tags=["practice-attempts"])
def get_practice_attempt(
    attempt_id: str,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    attempt = db.execute(
        "SELECT * FROM practice_attempts WHERE id = ? AND user_id = ?",
        (attempt_id, user["sub"]),
    ).fetchone()
    if attempt is None:
        raise _http(404, "Practice attempt not found")
    return _attempt_with_results(db, attempt)


@app.post("/api/v1/practice-attempts/{attempt_id}/corrections", tags=["practice-attempts"])
def create_attempt_correction(
    attempt_id: str,
    body: AttemptCorrectionBody,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    attempt = db.execute(
        """SELECT id FROM practice_attempts WHERE id = ? AND user_id = ? AND score_import_id IS NOT NULL""",
        (attempt_id, user["sub"]),
    ).fetchone()
    if attempt is None:
        raise _http(404, "Private practice attempt not found")
    now, correction_id = _iso(), _id()
    db.execute(
        """INSERT INTO attempt_corrections (id, user_id, attempt_id, subject, is_helpful, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (correction_id, user["sub"], attempt_id, body.subject.value, int(body.is_helpful), body.note, now),
    )
    return _output(db.execute("SELECT * FROM attempt_corrections WHERE id = ?", (correction_id,)).fetchone())


@app.post("/api/v1/practice-attempts/telemetry", tags=["practice-attempts"])
def record_pilot_telemetry(
    body: TelemetryBody,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not body.consent:
        raise _http(400, "Explicit consent is required for pilot telemetry")
    imported = db.execute(
        "SELECT id FROM score_imports WHERE id = ? AND user_id = ?",
        (body.score_import_id, user["sub"]),
    ).fetchone()
    if imported is None:
        raise _http(404, "Private arrangement not found")
    if body.attempt_id:
        attempt = db.execute(
            """SELECT id FROM practice_attempts WHERE id = ? AND user_id = ? AND score_import_id = ?""",
            (body.attempt_id, user["sub"], imported["id"]),
        ).fetchone()
        if attempt is None:
            raise _http(404, "Private practice attempt not found")
    now, event_id = _iso(), _id()
    db.execute(
        """INSERT INTO pilot_telemetry_events (id, user_id, score_import_id, attempt_id, event_type,
           outcome, reason, track_id, passage_start_measure, passage_end_measure, consented_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_id, user["sub"], imported["id"], body.attempt_id, body.event_type.value,
         body.outcome, body.reason, body.track_id, body.passage_start_measure,
         body.passage_end_measure, now, now),
    )
    return _output(db.execute("SELECT * FROM pilot_telemetry_events WHERE id = ?", (event_id,)).fetchone())


# Private score imports -----------------------------------------------------


MAX_SCORE_FILE_BYTES = 10 * 1024 * 1024


def _storage_error(error: StorageUnavailable) -> HTTPException:
    return _http(503, str(error))


def _safe_file_name(file_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", file_name)


def _validate_score_file_name(file_name: str, score_format: str) -> None:
    extension = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    supported = {
        "GUITAR_PRO": {"gp", "gp3", "gp4", "gp5", "gpx"},
        "MUSIC_XML": {"musicxml", "xml"},
        "ALPHA_TEX": {"alphatex", "at", "txt"},
    }
    if extension not in supported.get(score_format, set()):
        raise _http(400, "File extension does not match the selected score format")


def _import_detail(db: sqlite3.Connection, imported: sqlite3.Row) -> dict[str, object]:
    song = db.execute("SELECT id, title, artist FROM user_songs WHERE id = ?", (imported["user_song_id"],)).fetchone()
    track_rows = db.execute(
        "SELECT * FROM score_import_tracks WHERE score_import_id = ? ORDER BY track_index ASC",
        (imported["id"],),
    ).fetchall()
    return {
        "id": imported["id"],
        "status": imported["status"],
        "format": imported["format"],
        "originalFileName": imported["original_file_name"],
        "parseError": imported["parse_error"],
        "score": {"revision": score_revision(imported["event_version"], imported["source_hash"])},
        "createdAt": imported["created_at"],
        "updatedAt": imported["updated_at"],
        "song": _output(song),
        "timeline": _load_json(imported["timeline"]),
        "tracks": [
            {
                "id": row["id"],
                "sourceTrackId": row["source_track_id"],
                "trackIndex": row["track_index"],
                "name": row["name"],
                "instrument": row["instrument"],
                "tuning": _load_json(row["tuning"]),
                "capo": row["capo"],
            }
            for row in track_rows
        ],
    }


def _get_user_import(db: sqlite3.Connection, user_id: str, import_id: str) -> sqlite3.Row:
    imported = db.execute(
        "SELECT * FROM score_imports WHERE id = ? AND user_id = ?",
        (import_id, user_id),
    ).fetchone()
    if imported is None:
        raise _http(404, "Score import not found")
    return imported


def _timeline_preview(value: object) -> dict[str, object] | None:
    timeline = _load_json(value)
    if not isinstance(timeline, dict) or not isinstance(timeline.get("version"), int) or not isinstance(timeline.get("defaultBpm"), int):
        return None
    tracks = timeline.get("tracks")
    preview_tracks = []
    if isinstance(tracks, list):
        for track in tracks:
            if isinstance(track, dict):
                preview_tracks.append({key: track.get(key) for key in ("id", "index", "name", "instrument", "tuning")} | {"capo": track.get("capo")})
    return {"version": timeline["version"], "defaultBpm": timeline["defaultBpm"], "tracks": preview_tracks}


@app.post("/api/v1/score-imports", tags=["score-imports"])
def create_score_import(
    body: ScoreImportCreateBody,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    score_format = body.format.value
    _validate_score_file_name(body.file_name, score_format)
    now, song_id, import_id = _iso(), _id(), _id()
    title = body.title.strip()
    artist = body.artist.strip() if body.artist and body.artist.strip() else None
    storage_key = f"user-scores/{user['sub']}/{song_id}/{_safe_file_name(body.file_name)}"
    db.execute(
        "INSERT INTO user_songs (id, user_id, title, artist, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (song_id, user["sub"], title, artist, now, now),
    )
    db.execute(
        """INSERT INTO score_imports (id, user_song_id, user_id, status, format, original_file_name,
           mime_type, size_bytes, storage_key, created_at, updated_at)
           VALUES (?, ?, ?, 'UPLOAD_PENDING', ?, ?, ?, ?, ?, ?, ?)""",
        (import_id, song_id, user["sub"], score_format, body.file_name, body.mime_type,
         body.size_bytes, storage_key, now, now),
    )
    try:
        upload = ScoreStorage().create_signed_upload_url(storage_key, body.mime_type)
    except StorageUnavailable as error:
        raise _storage_error(error) from error
    imported = db.execute("SELECT * FROM score_imports WHERE id = ?", (import_id,)).fetchone()
    return {"song": _output(db.execute("SELECT * FROM user_songs WHERE id = ?", (song_id,)).fetchone()),
            "scoreImport": _output(imported), "upload": upload}


@app.post("/api/v1/score-imports/{import_id}/complete", tags=["score-imports"])
def complete_score_import(
    import_id: str,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    imported = _get_user_import(db, user["sub"], import_id)
    if imported["status"] == "READY":
        return _import_detail(db, imported)
    db.execute(
        "UPDATE score_imports SET status = 'PROCESSING', parse_error = NULL, updated_at = ? WHERE id = ?",
        (_iso(), import_id),
    )
    db.commit()
    try:
        data = ScoreStorage().download(imported["storage_key"])
        if not data:
            raise ScoreParseError("Uploaded score file is empty")
        if len(data) > MAX_SCORE_FILE_BYTES:
            raise OverflowError("Score file is too large")
        timeline = generate_from_bytes(
            format=imported["format"], data=data, default_bpm=120, time_signature="4/4"
        )
        digest = hashlib.sha256(data).hexdigest()
        now = _iso()
        db.execute(
            """UPDATE score_imports SET status = 'READY', source_hash = ?, event_version = ?, timeline = ?,
               parse_error = NULL, processed_at = ?, updated_at = ? WHERE id = ?""",
            (digest, timeline["version"], _json(timeline), now, now, import_id),
        )
        db.execute("DELETE FROM score_import_tracks WHERE score_import_id = ?", (import_id,))
        for track in timeline["tracks"]:
            db.execute(
                """INSERT INTO score_import_tracks (id, score_import_id, source_track_id, track_index,
                   name, instrument, tuning, capo, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (_id(), import_id, track["id"], track["index"], track["name"], track["instrument"],
                 _json(track["tuning"]), track.get("capo"), now),
            )
        db.commit()
    except OverflowError as error:
        message, code = str(error), 413
        db.rollback()
        now = _iso()
        db.execute(
            "UPDATE score_imports SET status = 'FAILED', parse_error = ?, processed_at = ?, updated_at = ? WHERE id = ?",
            (message, now, now, import_id),
        )
        db.commit()
        raise _http(code, message) from error
    except Exception as error:
        # Keep a safe parser diagnostic for the owner and make the failed state durable.
        message = str(error) if isinstance(error, (StorageUnavailable, ScoreParseError)) else "Score parsing failed"
        db.rollback()
        now = _iso()
        db.execute(
            "UPDATE score_imports SET status = 'FAILED', parse_error = ?, processed_at = ?, updated_at = ? WHERE id = ?",
            (message, now, now, import_id),
        )
        db.commit()
        raise _http(400, f"Score parsing failed: {message}") from error
    return _import_detail(db, db.execute("SELECT * FROM score_imports WHERE id = ?", (import_id,)).fetchone())


def _encode_import_cursor(created_at: str, import_id: str) -> str:
    payload = _json({"createdAt": created_at, "id": import_id}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_import_cursor(encoded: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        cursor = json.loads(raw)
        created_at, import_id = cursor["createdAt"], cursor["id"]
        _parse_date(created_at)
        if not isinstance(import_id, str) or not import_id:
            raise ValueError("missing import id")
        return created_at, import_id
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise _http(400, "Invalid score import cursor") from error


@app.get("/api/v1/score-imports", tags=["score-imports"])
def list_score_imports(
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = None,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    where = "si.user_id = ?"
    params: list[object] = [user["sub"]]
    if cursor:
        created_at, import_id = _decode_import_cursor(cursor)
        where += " AND (si.created_at < ? OR (si.created_at = ? AND si.id < ?))"
        params.extend([created_at, created_at, import_id])
    rows = db.execute(
        f"""SELECT si.*, us.id AS song_id, us.title AS song_title, us.artist AS song_artist
            FROM score_imports si JOIN user_songs us ON us.id = si.user_song_id
            WHERE {where} ORDER BY si.created_at DESC, si.id DESC LIMIT ?""",
        (*params, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    items = rows[:limit]
    response_items = []
    for imported in items:
        response_items.append({
            "id": imported["id"], "status": imported["status"], "format": imported["format"],
            "originalFileName": imported["original_file_name"], "parseError": imported["parse_error"],
            "createdAt": imported["created_at"], "updatedAt": imported["updated_at"],
            "song": {"id": imported["song_id"], "title": imported["song_title"], "artist": imported["song_artist"]},
            "timeline": _timeline_preview(imported["timeline"]),
        })
    final = items[-1] if items else None
    return {
        "items": response_items,
        "nextCursor": _encode_import_cursor(final["created_at"], final["id"]) if has_more and final else None,
    }


@app.get("/api/v1/score-imports/{import_id}/source", tags=["score-imports"])
def get_score_import_source(
    import_id: str,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    imported = _get_user_import(db, user["sub"], import_id)
    if imported["status"] != "READY":
        raise _http(400, "The score source is not ready for practice")
    try:
        signed = ScoreStorage().create_signed_download_url(imported["storage_key"])
    except StorageUnavailable as error:
        raise _storage_error(error) from error
    return {
        "id": imported["id"], "format": imported["format"],
        "originalFileName": imported["original_file_name"], **signed,
    }


@app.get("/api/v1/score-imports/{import_id}", tags=["score-imports"])
def get_score_import(
    import_id: str,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    return _import_detail(db, _get_user_import(db, user["sub"], import_id))


@app.delete("/api/v1/score-imports/{import_id}", tags=["score-imports"], status_code=status.HTTP_204_NO_CONTENT)
def delete_score_import(
    import_id: str,
    user: dict[str, str] = Depends(current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    imported = _get_user_import(db, user["sub"], import_id)
    try:
        ScoreStorage().remove(imported["storage_key"])
    except StorageUnavailable as error:
        raise _storage_error(error) from error
    db.execute("DELETE FROM user_songs WHERE id = ?", (imported["user_song_id"],))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
