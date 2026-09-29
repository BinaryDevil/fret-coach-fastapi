from __future__ import annotations

import sqlite3
from collections.abc import Generator

from app.config import database_path


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    google_subject TEXT UNIQUE,
    display_name TEXT,
    handedness TEXT NOT NULL DEFAULT 'RIGHT',
    tuning TEXT NOT NULL DEFAULT 'E2,A2,D3,G3,B3,E4',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    refresh_token_hash TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_progress (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    total_sessions INTEGER NOT NULL DEFAULT 0,
    total_practice_seconds INTEGER NOT NULL DEFAULT 0,
    current_day_streak INTEGER NOT NULL DEFAULT 0,
    longest_day_streak INTEGER NOT NULL DEFAULT 0,
    last_practiced_on TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exercises (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    difficulty TEXT NOT NULL DEFAULT 'BEGINNER',
    title TEXT NOT NULL,
    description TEXT,
    instructions TEXT,
    skill_focus TEXT NOT NULL DEFAULT 'Technique fundamentals',
    estimated_minutes INTEGER NOT NULL DEFAULT 8,
    library_order INTEGER NOT NULL DEFAULT 0,
    configuration TEXT NOT NULL,
    reference_audio_url TEXT,
    is_published INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exercise_scores (
    id TEXT PRIMARY KEY,
    exercise_id TEXT NOT NULL UNIQUE REFERENCES exercises(id) ON DELETE CASCADE,
    format TEXT NOT NULL,
    file_name TEXT,
    storage_key TEXT,
    content TEXT,
    time_signature TEXT NOT NULL DEFAULT '4/4',
    default_bpm INTEGER NOT NULL,
    total_measures INTEGER NOT NULL,
    backing_track_url TEXT,
    backing_track_offset_ms INTEGER NOT NULL DEFAULT 0,
    event_version INTEGER NOT NULL DEFAULT 0,
    event_source_hash TEXT,
    timeline TEXT,
    event_generation_error TEXT,
    event_generated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loop_regions (
    id TEXT PRIMARY KEY,
    exercise_id TEXT NOT NULL REFERENCES exercises(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    start_measure INTEGER NOT NULL,
    end_measure INTEGER NOT NULL,
    default_bpm INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(exercise_id, start_measure, end_measure)
);
CREATE INDEX IF NOT EXISTS idx_loop_regions_exercise_start ON loop_regions(exercise_id, start_measure);

CREATE TABLE IF NOT EXISTS practice_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    exercise_id TEXT REFERENCES exercises(id) ON DELETE SET NULL,
    piece_title TEXT,
    sheet_music_url TEXT,
    backing_track_url TEXT,
    loop_region_id TEXT REFERENCES loop_regions(id) ON DELETE SET NULL,
    target_bpm INTEGER,
    duration_seconds INTEGER,
    status TEXT NOT NULL DEFAULT 'IN_PROGRESS',
    started_at TEXT NOT NULL,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user_started ON practice_sessions(user_id, started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_exercise ON practice_sessions(exercise_id);

CREATE TABLE IF NOT EXISTS performance_recordings (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES practice_sessions(id) ON DELETE CASCADE,
    storage_key TEXT NOT NULL UNIQUE,
    mime_type TEXT NOT NULL,
    duration_ms INTEGER,
    recorded_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS performance_analyses (
    id TEXT PRIMARY KEY,
    recording_id TEXT NOT NULL UNIQUE REFERENCES performance_recordings(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    model_version TEXT,
    overall_score REAL,
    pitch_score REAL,
    rhythm_score REAL,
    technique_score REAL,
    summary TEXT,
    error_message TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS performance_feedback (
    id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL REFERENCES performance_analyses(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    start_ms INTEGER,
    end_ms INTEGER,
    message TEXT NOT NULL,
    suggestion TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_songs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    artist TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_songs_user_updated ON user_songs(user_id, updated_at);

CREATE TABLE IF NOT EXISTS score_imports (
    id TEXT PRIMARY KEY,
    user_song_id TEXT NOT NULL REFERENCES user_songs(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'UPLOAD_PENDING',
    format TEXT NOT NULL,
    original_file_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    source_hash TEXT,
    event_version INTEGER NOT NULL DEFAULT 0,
    timeline TEXT,
    parse_error TEXT,
    processed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_score_imports_user_created ON score_imports(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_score_imports_song_created ON score_imports(user_song_id, created_at);

CREATE TABLE IF NOT EXISTS score_import_tracks (
    id TEXT PRIMARY KEY,
    score_import_id TEXT NOT NULL REFERENCES score_imports(id) ON DELETE CASCADE,
    source_track_id TEXT NOT NULL,
    track_index INTEGER NOT NULL,
    name TEXT NOT NULL,
    instrument TEXT NOT NULL,
    tuning TEXT NOT NULL,
    capo INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(score_import_id, source_track_id)
);

CREATE TABLE IF NOT EXISTS practice_attempts (
    id TEXT PRIMARY KEY,
    client_run_id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    exercise_id TEXT REFERENCES exercises(id) ON DELETE CASCADE,
    score_import_id TEXT REFERENCES score_imports(id) ON DELETE CASCADE,
    loop_region_id TEXT REFERENCES loop_regions(id) ON DELETE SET NULL,
    track_id TEXT,
    passage_start_measure INTEGER,
    passage_end_measure INTEGER,
    score_revision TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'COMPLETED',
    bpm INTEGER NOT NULL,
    count_in_beats INTEGER NOT NULL,
    active_duration_ms INTEGER NOT NULL,
    total_target_events INTEGER NOT NULL,
    correct_count INTEGER NOT NULL,
    wrong_pitch_count INTEGER NOT NULL,
    missing_count INTEGER NOT NULL,
    extra_note_count INTEGER NOT NULL DEFAULT 0,
    accuracy_percent INTEGER NOT NULL,
    started_at TEXT,
    completed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_user_completed ON practice_attempts(user_id, completed_at);
CREATE INDEX IF NOT EXISTS idx_attempts_user_exercise_completed ON practice_attempts(user_id, exercise_id, completed_at);
CREATE INDEX IF NOT EXISTS idx_attempts_user_import_completed ON practice_attempts(user_id, score_import_id, completed_at);

CREATE TABLE IF NOT EXISTS practice_note_results (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES practice_attempts(id) ON DELETE CASCADE,
    timeline_event_id TEXT NOT NULL,
    event_index INTEGER NOT NULL,
    measure INTEGER NOT NULL,
    beat INTEGER NOT NULL,
    start_tick INTEGER NOT NULL,
    expected_pitches TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detected_midi INTEGER,
    corrected_midi INTEGER,
    cents_offset INTEGER,
    octave_shift INTEGER,
    confidence REAL,
    timing_offset_ms INTEGER,
    bend TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(attempt_id, timeline_event_id)
);
CREATE INDEX IF NOT EXISTS idx_note_results_attempt_event ON practice_note_results(attempt_id, event_index);

CREATE TABLE IF NOT EXISTS attempt_corrections (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES practice_attempts(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    is_helpful INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corrections_attempt_created ON attempt_corrections(attempt_id, created_at);

CREATE TABLE IF NOT EXISTS pilot_telemetry_events (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    score_import_id TEXT NOT NULL REFERENCES score_imports(id) ON DELETE CASCADE,
    attempt_id TEXT REFERENCES practice_attempts(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    outcome TEXT,
    reason TEXT,
    track_id TEXT,
    passage_start_measure INTEGER,
    passage_end_measure INTEGER,
    consented_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_import_created ON pilot_telemetry_events(score_import_id, created_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_user_created ON pilot_telemetry_events(user_id, created_at);

CREATE TABLE IF NOT EXISTS user_exercise_progress (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    exercise_id TEXT NOT NULL REFERENCES exercises(id) ON DELETE CASCADE,
    completed_passes INTEGER NOT NULL DEFAULT 0,
    total_active_ms INTEGER NOT NULL DEFAULT 0,
    best_accuracy_percent INTEGER NOT NULL DEFAULT 0,
    best_bpm INTEGER NOT NULL DEFAULT 0,
    latest_accuracy_percent INTEGER,
    last_practiced_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, exercise_id)
);
CREATE INDEX IF NOT EXISTS idx_user_exercise_progress_last ON user_exercise_progress(user_id, last_practiced_at);
"""


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database() -> None:
    connection = connect()
    try:
        connection.executescript(SCHEMA)
    finally:
        connection.close()


def get_db() -> Generator[sqlite3.Connection, None, None]:
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
