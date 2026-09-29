from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


def to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        use_enum_values=False,
    )


class ExerciseType(StrEnum):
    CHROMATIC = "CHROMATIC"
    SCALE = "SCALE"
    FINGER_DEXTERITY = "FINGER_DEXTERITY"


class Difficulty(StrEnum):
    BEGINNER = "BEGINNER"
    INTERMEDIATE = "INTERMEDIATE"
    ADVANCED = "ADVANCED"


class Handedness(StrEnum):
    RIGHT = "RIGHT"
    LEFT = "LEFT"


class ScoreFormat(StrEnum):
    GUITAR_PRO = "GUITAR_PRO"
    MUSIC_XML = "MUSIC_XML"
    ALPHA_TEX = "ALPHA_TEX"


class PracticeNoteOutcome(StrEnum):
    CORRECT = "CORRECT"
    WRONG_PITCH = "WRONG_PITCH"
    MISSING = "MISSING"
    EXTRA_NOTE = "EXTRA_NOTE"


class AttemptCorrectionSubject(StrEnum):
    DIAGNOSIS = "DIAGNOSIS"
    INTERVENTION = "INTERVENTION"


class PilotTelemetryEventType(StrEnum):
    SETUP_OUTCOME = "SETUP_OUTCOME"
    INPUT_QUALITY_OUTCOME = "INPUT_QUALITY_OUTCOME"
    PASSAGE_SELECTED = "PASSAGE_SELECTED"
    ATTEMPT_STARTED = "ATTEMPT_STARTED"
    ATTEMPT_FINISHED = "ATTEMPT_FINISHED"
    ATTEMPT_ABANDONED = "ATTEMPT_ABANDONED"
    ATTEMPT_UNASSESSABLE = "ATTEMPT_UNASSESSABLE"
    REVIEW_SHOWN = "REVIEW_SHOWN"
    CORRECTION_SUBMITTED = "CORRECTION_SUBMITTED"
    RETURNED = "RETURNED"


class RegisterBody(ApiModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    display_name: str | None = Field(default=None, max_length=80)


class LoginBody(ApiModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)


class GoogleLoginBody(ApiModel):
    credential: str = Field(min_length=20)


class RefreshBody(ApiModel):
    refresh_token: str


class UpdateProfileBody(ApiModel):
    display_name: str | None = Field(default=None, max_length=80)
    handedness: Handedness | None = None
    tuning: str | None = Field(default=None, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)


class SessionCreateBody(ApiModel):
    exercise_id: str
    loop_region_id: str | None = None
    target_bpm: int | None = Field(default=None, ge=20, le=400)


class RecordingCreateBody(ApiModel):
    storage_key: str = Field(max_length=500)
    mime_type: str = Field(max_length=100)
    duration_ms: int | None = Field(default=None, ge=1)


class BendDiagnostic(ApiModel):
    reached_target: bool
    peak_midi: float = Field(ge=0, le=127)
    target_cents_error: float = Field(ge=-2400, le=2400)
    reached_target_at_ms: int | None = Field(default=None, ge=0)
    hold_duration_ms: int = Field(ge=0)


class PracticeNoteResultBody(ApiModel):
    timeline_event_id: str
    event_index: int = Field(ge=0)
    outcome: PracticeNoteOutcome
    detected_midi: int | None = Field(default=None, ge=0, le=127)
    corrected_midi: int | None = Field(default=None, ge=0, le=127)
    cents_offset: int | None = None
    octave_shift: int | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    timing_offset_ms: int | None = None
    bend: BendDiagnostic | None = None


class PracticeAttemptCreateBody(ApiModel):
    client_run_id: UUID
    exercise_id: str | None = None
    score_import_id: str | None = None
    track_id: str | None = None
    passage_start_measure: int | None = Field(default=None, ge=1)
    passage_end_measure: int | None = Field(default=None, ge=1)
    loop_region_id: str | None = None
    score_revision: str
    bpm: int = Field(ge=20, le=400)
    count_in_beats: Literal[0, 2, 4]
    active_duration_ms: int = Field(ge=0, le=86_400_000)
    started_at: datetime | None = None
    note_results: list[PracticeNoteResultBody] = Field(max_length=10_000)


class AttemptCorrectionBody(ApiModel):
    subject: AttemptCorrectionSubject
    is_helpful: bool
    note: str | None = Field(default=None, max_length=1000)


class TelemetryBody(ApiModel):
    consent: bool
    score_import_id: str
    attempt_id: str | None = None
    event_type: PilotTelemetryEventType
    outcome: str | None = Field(default=None, max_length=80)
    reason: str | None = Field(default=None, max_length=200)
    track_id: str | None = None
    passage_start_measure: int | None = Field(default=None, ge=1, le=10_000)
    passage_end_measure: int | None = Field(default=None, ge=1, le=10_000)


class ScoreImportCreateBody(ApiModel):
    title: str = Field(max_length=160)
    artist: str | None = Field(default=None, max_length=160)
    format: ScoreFormat
    file_name: str = Field(max_length=255)
    mime_type: str
    size_bytes: int = Field(ge=1, le=10 * 1024 * 1024)

    @field_validator("mime_type")
    @classmethod
    def mime_type_must_be_well_formed(cls, value: str) -> str:
        if not re.fullmatch(r"[\w.+-]+/[\w.+-]+", value):
            raise ValueError("mimeType must be a valid media type")
        return value

    @field_validator("title", "file_name")
    @classmethod
    def required_text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class AttemptListQuery(ApiModel):
    exercise_id: str | None = None
    score_import_id: str | None = None
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class ScoreImportListQuery(ApiModel):
    limit: int = Field(default=30, ge=1, le=100)
    cursor: str | None = None


class PrivateHistoryQuery(ApiModel):
    track_id: str
    passage_start_measure: int = Field(ge=1, le=10_000)
    passage_end_measure: int = Field(ge=1, le=10_000)

    @model_validator(mode="after")
    def range_must_be_forward(self) -> PrivateHistoryQuery:
        if self.passage_start_measure > self.passage_end_measure:
            raise ValueError("The selected passage range is invalid")
        return self
