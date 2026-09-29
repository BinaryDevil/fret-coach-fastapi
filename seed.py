from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from app.database import connect, initialize_database
from app.score_timeline import generate_from_content, source_hash


EXERCISES = [
    {
        "slug": "chromatic-1-2-3-4",
        "type": "CHROMATIC",
        "difficulty": "BEGINNER",
        "title": "1-2-3-4 Full-Range Spider",
        "skill_focus": "Finger independence",
        "estimated_minutes": 8,
        "target_bpm": 60,
        "bpm_range": [40, 140],
        "description": "A full-range chromatic spider from low E to high E and back.",
        "instructions": "Play evenly with one finger per fret. Raise the tempo only after a clean pass.",
        "body": ":4 1.6 2.6 3.6 4.6 | 1.5 2.5 3.5 4.5 | 1.4 2.4 3.4 4.4 | 1.3 2.3 3.3 4.3 | 1.2 2.2 3.2 4.2 | 1.1 2.1 3.1 4.1 | 4.1 3.1 2.1 1.1 | 4.2 3.2 2.2 1.2 | 4.3 3.3 2.3 1.3 | 4.4 3.4 2.4 1.4 | 4.5 3.5 2.5 1.5 | 4.6 3.6 2.6 1.6 |",
    },
    {
        "slug": "alternate-picking-single-string",
        "type": "FINGER_DEXTERITY",
        "difficulty": "BEGINNER",
        "title": "Alternate Picking Pulse",
        "skill_focus": "Picking control",
        "estimated_minutes": 6,
        "target_bpm": 72,
        "bpm_range": [44, 168],
        "description": "A repeating single-string line for even alternate picking.",
        "instructions": "Alternate down-up continuously. Keep pick motion short and consistent.",
        "body": ":8 1.6 {sd} 2.6 {su} 3.6 {sd} 4.6 {su} 4.6 {sd} 3.6 {su} 2.6 {sd} 1.6 {su} | 1.5 {sd} 2.5 {su} 3.5 {sd} 4.5 {su} 4.5 {sd} 3.5 {su} 2.5 {sd} 1.5 {su} |",
    },
    {
        "slug": "open-chord-triad-switches",
        "type": "FINGER_DEXTERITY",
        "difficulty": "BEGINNER",
        "title": "Open Triad Switches",
        "skill_focus": "Clean transitions",
        "estimated_minutes": 8,
        "target_bpm": 56,
        "bpm_range": [36, 120],
        "description": "Simple three-note chord shapes for calm, deliberate changes.",
        "instructions": "Form each shape before striking it. Let every note ring without rushing the next change.",
        "body": ":4 (0.4 1.3 0.2) (2.5 2.4 1.3) (0.4 0.3 0.2) (2.4 1.3 0.2) | (0.4 1.3 0.2) (2.5 2.4 1.3) (0.4 0.3 0.2) (2.4 1.3 0.2) |",
    },
    {
        "slug": "whole-step-bend-targets",
        "type": "FINGER_DEXTERITY",
        "difficulty": "INTERMEDIATE",
        "title": "Whole-Step Bend Targets",
        "skill_focus": "Bend intonation",
        "estimated_minutes": 6,
        "target_bpm": 52,
        "bpm_range": [32, 104],
        "description": "Reference notes and target frets for future bend-pitch feedback.",
        "instructions": "Play the target note, then match it from two frets below with a controlled bend.",
        "body": ":4 7.2 5.2{b (0 8)} 7.2 5.2{b (0 8)} | 8.2 6.2{b (0 8)} 8.2 6.2{b (0 8)} | 10.2 8.2{b (0 8)} 10.2 8.2{b (0 8)} | 12.2 10.2{b (0 8)} 12.2 10.2{b (0 8)} |",
    },
]


def main() -> None:
    initialize_database()
    db = connect()
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    try:
        for order, definition in enumerate(EXERCISES, start=1):
            exercise = db.execute("SELECT id FROM exercises WHERE slug = ?", (definition["slug"],)).fetchone()
            exercise_id = exercise["id"] if exercise else str(uuid4())
            configuration = {
                "targetBpm": definition["target_bpm"],
                "bpmRange": definition["bpm_range"],
                "tags": [definition["skill_focus"].lower(), definition["type"].lower()],
            }
            db.execute(
                """INSERT INTO exercises (id, slug, type, difficulty, title, description, instructions,
                   skill_focus, estimated_minutes, library_order, configuration, is_published, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET type=excluded.type, difficulty=excluded.difficulty,
                   title=excluded.title, description=excluded.description, instructions=excluded.instructions,
                   skill_focus=excluded.skill_focus, estimated_minutes=excluded.estimated_minutes,
                   library_order=excluded.library_order, configuration=excluded.configuration,
                   is_published=1, updated_at=excluded.updated_at""",
                (exercise_id, definition["slug"], definition["type"], definition["difficulty"],
                 definition["title"], definition["description"], definition["instructions"],
                 definition["skill_focus"], definition["estimated_minutes"], order,
                 json.dumps(configuration), now, now),
            )
            content = f'\\title "{definition["title"]}"\n\\tempo {definition["target_bpm"]}\n.\n{definition["body"]}'
            timeline = generate_from_content(
                format="ALPHA_TEX", content=content, default_bpm=definition["target_bpm"], time_signature="4/4"
            )
            digest = source_hash(
                format="ALPHA_TEX", content=content, storage_key=None,
                default_bpm=definition["target_bpm"], time_signature="4/4",
            )
            existing_score = db.execute("SELECT id FROM exercise_scores WHERE exercise_id = ?", (exercise_id,)).fetchone()
            score_id = existing_score["id"] if existing_score else str(uuid4())
            db.execute(
                """INSERT INTO exercise_scores (id, exercise_id, format, content, time_signature,
                   default_bpm, total_measures, event_version, event_source_hash, timeline, event_generated_at,
                   created_at, updated_at) VALUES (?, ?, 'ALPHA_TEX', ?, '4/4', ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(exercise_id) DO UPDATE SET content=excluded.content, default_bpm=excluded.default_bpm,
                   total_measures=excluded.total_measures, event_version=excluded.event_version,
                   event_source_hash=excluded.event_source_hash, timeline=excluded.timeline,
                   event_generated_at=excluded.event_generated_at, updated_at=excluded.updated_at""",
                (score_id, exercise_id, content, definition["target_bpm"], timeline["totalMeasures"],
                 timeline["version"], digest, json.dumps(timeline), now, now, now),
            )
            db.execute(
                """INSERT INTO loop_regions (id, exercise_id, title, start_measure, end_measure,
                   default_bpm, created_at, updated_at) VALUES (?, ?, 'Full Loop', 1, ?, ?, ?, ?)
                   ON CONFLICT(exercise_id, start_measure, end_measure) DO UPDATE SET
                   title='Full Loop', default_bpm=excluded.default_bpm, updated_at=excluded.updated_at""",
                (str(uuid4()), exercise_id, timeline["totalMeasures"],
                 definition["target_bpm"], now, now),
            )
        db.commit()
        print(f"Seeded {len(EXERCISES)} demo exercises.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
