from __future__ import annotations

from uuid import uuid4

import pytest

CHROMATIC = "chromatic-1-2-3-4"
TRIADS = "open-chord-triad-switches"


def _events(exercise: dict) -> list[dict]:
    return exercise["score"]["timeline"]["events"]


def _result(event_index: int, event: dict, outcome: str = "CORRECT", **extra) -> dict:
    return {"timelineEventId": event["id"], "eventIndex": event_index, "outcome": outcome, **extra}


def _exercise_attempt(exercise: dict, outcomes: list[str] | None = None, **overrides) -> dict:
    """Build a body; ``outcomes[i]`` is the outcome for event i, and omitted events are left unsubmitted."""
    events = _events(exercise)
    outcomes = ["CORRECT"] * len(events) if outcomes is None else outcomes
    return {
        "clientRunId": str(uuid4()),
        "exerciseId": exercise["id"],
        "scoreRevision": exercise["score"]["revision"],
        "bpm": 60,
        "countInBeats": 4,
        "activeDurationMs": 30_000,
        "noteResults": [_result(i, events[i], outcome) for i, outcome in enumerate(outcomes) if outcome],
        **overrides,
    }


@pytest.fixture
def post_attempt(client):
    def _post(headers: dict, body: dict):
        return client.post("/api/v1/practice-attempts", headers=headers, json=body)

    return _post


class TestExerciseAttemptScoring:
    def test_all_correct(self, seeded, auth, post_attempt):
        exercise = seeded[CHROMATIC]
        response = post_attempt(auth, _exercise_attempt(exercise))
        assert response.status_code == 200
        body = response.json()
        assert body["totalTargetEvents"] == 48
        assert body["correctCount"] == 48
        assert body["accuracyPercent"] == 100
        assert body["scoreRevision"] == exercise["score"]["revision"]
        assert [r["eventIndex"] for r in body["noteResults"]] == list(range(48))
        assert body["noteResults"][0]["expectedPitches"] == [41]

    def test_mixed_outcomes_and_unsubmitted_events(self, seeded, auth, post_attempt):
        # Triads has 8 events: 5 correct, 1 wrong, 1 extra-note, 1 not submitted.
        outcomes = ["CORRECT"] * 5 + ["WRONG_PITCH", "EXTRA_NOTE", None]
        body = post_attempt(auth, _exercise_attempt(seeded[TRIADS], outcomes)).json()
        assert (body["correctCount"], body["wrongPitchCount"], body["missingCount"], body["extraNoteCount"]) == (5, 1, 2, 1)
        assert body["accuracyPercent"] == 63  # 62.5 rounds half up
        assert [r["outcome"] for r in body["noteResults"]][-1] == "MISSING"

    def test_accuracy_rounds_half_up(self, seeded, auth, post_attempt):
        body = post_attempt(auth, _exercise_attempt(seeded[TRIADS], ["CORRECT"])).json()
        assert body["accuracyPercent"] == 13  # 12.5%

    def test_note_diagnostics_are_persisted(self, seeded, auth, post_attempt):
        exercise = seeded[TRIADS]
        bend = {"reachedTarget": True, "peakMidi": 61.5, "targetCentsError": -12.0, "holdDurationMs": 200}
        body = _exercise_attempt(exercise, [])
        body["noteResults"] = [
            _result(0, _events(exercise)[0], "WRONG_PITCH", detectedMidi=51, centsOffset=-20, confidence=0.8, bend=bend)
        ]
        note = post_attempt(auth, body).json()["noteResults"][0]
        assert (note["detectedMidi"], note["centsOffset"], note["confidence"]) == (51, -20, 0.8)
        assert note["bend"] == {**bend, "reachedTargetAtMs": None}

    def test_loop_region_limits_targets(self, seeded, auth, post_attempt, db):
        exercise = seeded[CHROMATIC]
        loop_id = str(uuid4())
        db.execute(
            "INSERT INTO loop_regions (id, exercise_id, title, start_measure, end_measure, created_at, updated_at)"
            " VALUES (?, ?, 'Bars 2-3', 2, 3, 'now', 'now')",
            (loop_id, exercise["id"]),
        )
        db.commit()
        body = post_attempt(auth, _exercise_attempt(exercise, [], loopRegionId=loop_id)).json()
        assert body["totalTargetEvents"] == 8
        assert body["accuracyPercent"] == 0
        assert {r["measure"] for r in body["noteResults"]} == {2, 3}

    def test_updates_exercise_progress_and_dashboard(self, client, seeded, auth, post_attempt):
        exercise = seeded[CHROMATIC]
        post_attempt(auth, _exercise_attempt(exercise, bpm=60))
        post_attempt(auth, _exercise_attempt(exercise, ["CORRECT"] * 24, bpm=80))
        progress = client.get(f"/api/v1/exercises/{exercise['id']}/progress", headers=auth).json()
        assert progress["completedPasses"] == 2
        assert progress["totalActiveMs"] == 60_000
        assert progress["bestAccuracyPercent"] == 100
        assert progress["bestBpm"] == 60  # 80 BPM only scored 50%, below the 90% bar
        assert progress["latestAccuracyPercent"] == 50

        dashboard = client.get("/api/v1/dashboard/me", headers=auth).json()
        assert dashboard["stats"]["averageAccuracyPercent"] == 75
        assert dashboard["stats"]["playedThisWeekMs"] == 60_000
        assert dashboard["nextPractice"]["id"] == exercise["id"]
        assert dashboard["nextPractice"]["targetBpm"] == 80
        assert dashboard["coachNote"].startswith("Slow the tempo")

    @pytest.mark.parametrize(
        ("correct", "note"),
        [(48, "Strong last pass. Try 64 BPM"), (40, "Your accuracy is building"), (10, "Slow the tempo")],
    )
    def test_coach_note_tiers(self, client, seeded, auth, post_attempt, correct, note):
        post_attempt(auth, _exercise_attempt(seeded[CHROMATIC], ["CORRECT"] * correct))
        assert client.get("/api/v1/dashboard/me", headers=auth).json()["coachNote"].startswith(note)


class TestExerciseAttemptValidation:
    def test_retry_with_same_client_run_id_is_idempotent(self, client, seeded, auth, post_attempt):
        exercise = seeded[TRIADS]
        body = _exercise_attempt(exercise)
        first = post_attempt(auth, body).json()
        second = post_attempt(auth, {**body, "noteResults": []}).json()
        assert second == first
        progress = client.get(f"/api/v1/exercises/{exercise['id']}/progress", headers=auth).json()
        assert progress["completedPasses"] == 1

    def test_client_run_id_is_not_shared_across_users(self, seeded, auth, register, post_attempt):
        body = _exercise_attempt(seeded[TRIADS])
        post_attempt(auth, body)
        assert post_attempt(register()["headers"], body).status_code == 409

    @pytest.mark.parametrize(
        ("overrides", "status"),
        [
            ({"scoreRevision": "v3:stale"}, 409),
            ({"bpm": 150}, 400),  # chromatic range is 40-140
            ({"bpm": 39}, 400),
            ({"exerciseId": None}, 400),
            ({"scoreImportId": "import-1"}, 400),
            ({"trackId": "t0"}, 400),
            ({"passageStartMeasure": 1}, 400),
            ({"loopRegionId": "not-a-loop"}, 400),
            ({"exerciseId": "missing"}, 404),
            ({"countInBeats": 3}, 422),
            ({"clientRunId": "not-a-uuid"}, 422),
            ({"bpm": 401}, 422),
        ],
    )
    def test_rejects_invalid_bodies(self, seeded, auth, post_attempt, overrides, status):
        assert post_attempt(auth, _exercise_attempt(seeded[CHROMATIC], **overrides)).status_code == status

    def test_rejects_bad_note_results(self, seeded, auth, post_attempt):
        exercise = seeded[TRIADS]
        events = _events(exercise)
        cases = [
            [{"timelineEventId": "m99-b1", "eventIndex": 0, "outcome": "CORRECT"}],
            [_result(0, events[0]), _result(0, events[0])],
            [_result(5, events[0])],
        ]
        for note_results in cases:
            body = _exercise_attempt(exercise, noteResults=note_results)
            assert post_attempt(auth, body).status_code == 400

    def test_exercise_without_scoreable_events(self, seeded, auth, post_attempt, db):
        exercise = seeded[TRIADS]
        db.execute("UPDATE exercise_scores SET timeline = ? WHERE exercise_id = ?", ('{"version": 3, "events": []}', exercise["id"]))
        db.commit()
        response = post_attempt(auth, _exercise_attempt(exercise, noteResults=[]))
        assert response.status_code == 400
        assert "no scoreable note events" in response.json()["detail"]


class TestListAndGet:
    def test_pagination_and_filters(self, client, seeded, auth, post_attempt):
        ids = [post_attempt(auth, _exercise_attempt(seeded[TRIADS])).json()["id"] for _ in range(3)]
        other = post_attempt(auth, _exercise_attempt(seeded[CHROMATIC])).json()["id"]

        page = client.get(f"/api/v1/practice-attempts?exerciseId={seeded[TRIADS]['id']}&limit=2", headers=auth).json()
        assert len(page["items"]) == 2 and page["nextCursor"] == page["items"][-1]["id"]
        rest = client.get(
            f"/api/v1/practice-attempts?exerciseId={seeded[TRIADS]['id']}&limit=2&cursor={page['nextCursor']}", headers=auth
        ).json()
        assert rest["nextCursor"] is None
        assert {item["id"] for item in page["items"] + rest["items"]} == set(ids)

        everything = client.get("/api/v1/practice-attempts", headers=auth).json()["items"]
        assert {item["id"] for item in everything} == {*ids, other}
        assert "noteResults" not in everything[0]

    def test_invalid_cursor_and_limit(self, client, auth):
        assert client.get("/api/v1/practice-attempts?cursor=missing", headers=auth).status_code == 400
        assert client.get("/api/v1/practice-attempts?limit=0", headers=auth).status_code == 422

    def test_get_is_scoped_to_owner(self, client, seeded, auth, register, post_attempt):
        attempt = post_attempt(auth, _exercise_attempt(seeded[TRIADS])).json()
        assert client.get(f"/api/v1/practice-attempts/{attempt['id']}", headers=auth).json() == attempt
        intruder = register()["headers"]
        assert client.get(f"/api/v1/practice-attempts/{attempt['id']}", headers=intruder).status_code == 404
        assert client.get("/api/v1/practice-attempts", headers=intruder).json()["items"] == []


def _private_attempt(imported: dict, *, start: int = 1, end: int = 1, outcomes: list[str] | None = None, **overrides) -> dict:
    events = [
        (index, event) for index, event in enumerate(imported["timeline"]["events"])
        if start <= event["measure"] <= end and not event["isRest"]
    ]
    outcomes = ["CORRECT"] * len(events) if outcomes is None else outcomes
    return {
        "clientRunId": str(uuid4()),
        "scoreImportId": imported["id"],
        "trackId": "t0",
        "passageStartMeasure": start,
        "passageEndMeasure": end,
        "scoreRevision": imported["score"]["revision"],
        "bpm": 100,
        "countInBeats": 2,
        "activeDurationMs": 10_000,
        "noteResults": [_result(index, event, outcome) for (index, event), outcome in zip(events, outcomes)],
        **overrides,
    }


class TestPrivateAttempts:
    @pytest.fixture
    def imported(self, create_import, auth):
        return create_import(auth)

    def test_scores_selected_passage(self, client, auth, imported, post_attempt, db):
        response = post_attempt(auth, _private_attempt(imported, start=2, end=3, outcomes=["CORRECT", "CORRECT", "WRONG_PITCH"]))
        assert response.status_code == 200
        body = response.json()
        assert body["totalTargetEvents"] == 5  # 3 notes in bar 2 (rest excluded), chord + note in bar 3
        assert (body["correctCount"], body["wrongPitchCount"], body["missingCount"]) == (2, 1, 2)
        assert body["accuracyPercent"] == 40
        assert (body["trackId"], body["passageStartMeasure"], body["passageEndMeasure"]) == ("t0", 2, 3)
        assert body["exerciseId"] is None
        assert db.execute("SELECT COUNT(*) FROM user_exercise_progress").fetchone()[0] == 0

    @pytest.mark.parametrize(
        ("overrides", "status"),
        [
            ({"trackId": "t9"}, 400),
            ({"trackId": None}, 400),
            ({"passageStartMeasure": 3, "passageEndMeasure": 2}, 400),
            ({"passageEndMeasure": 4}, 400),
            ({"loopRegionId": "loop"}, 400),
            ({"exerciseId": "exercise"}, 400),
            ({"scoreRevision": "v3:old"}, 409),
            ({"scoreImportId": "missing"}, 404),
        ],
    )
    def test_rejects_invalid_private_bodies(self, auth, imported, post_attempt, overrides, status):
        assert post_attempt(auth, _private_attempt(imported, **overrides)).status_code == status

    def test_requires_ready_import_owned_by_user(self, auth, register, create_import, imported, post_attempt):
        pending = create_import(auth, complete=False)["scoreImport"]
        body = _private_attempt(imported, scoreImportId=pending["id"])
        assert post_attempt(auth, body).status_code == 404
        assert post_attempt(register()["headers"], _private_attempt(imported)).status_code == 404

    def test_history_corrections_and_telemetry(self, client, auth, register, imported, post_attempt):
        first = post_attempt(auth, _private_attempt(imported, outcomes=["CORRECT"])).json()
        second = post_attempt(auth, _private_attempt(imported)).json()
        post_attempt(auth, _private_attempt(imported, start=2, end=2))  # different passage, excluded

        correction = client.post(
            f"/api/v1/practice-attempts/{first['id']}/corrections",
            headers=auth,
            json={"subject": "DIAGNOSIS", "isHelpful": False, "note": "Mic picked up noise"},
        )
        assert correction.status_code == 200
        assert correction.json()["isHelpful"] is False

        history_url = f"/api/v1/practice-attempts/private/{imported['id']}/history"
        params = {"trackId": "t0", "passageStartMeasure": 1, "passageEndMeasure": 1}
        history = client.get(history_url, headers=auth, params=params).json()
        assert history["baselineAttemptId"] == first["id"]
        assert [item["id"] for item in history["items"]] == [first["id"], second["id"]]
        assert history["items"][0]["corrections"][0]["note"] == "Mic picked up noise"
        assert history["items"][1]["corrections"] == []

        assert client.get(history_url, headers=register()["headers"], params=params).status_code == 404

        telemetry = client.post(
            "/api/v1/practice-attempts/telemetry",
            headers=auth,
            json={"consent": True, "scoreImportId": imported["id"], "attemptId": first["id"],
                  "eventType": "REVIEW_SHOWN", "trackId": "t0"},
        )
        assert telemetry.status_code == 200
        assert telemetry.json()["eventType"] == "REVIEW_SHOWN"

    @pytest.mark.xfail(
        strict=True,
        raises=Exception,
        reason="Bug: PrivateHistoryQuery is injected with Depends(), so its model_validator raises an "
        "unhandled ValidationError (500) instead of FastAPI returning 422.",
    )
    def test_history_rejects_backwards_range(self, client, auth, imported):
        response = client.get(
            f"/api/v1/practice-attempts/private/{imported['id']}/history",
            headers=auth, params={"trackId": "t0", "passageStartMeasure": 2, "passageEndMeasure": 1},
        )
        assert response.status_code == 422

    def test_history_empty(self, client, auth, imported):
        response = client.get(
            f"/api/v1/practice-attempts/private/{imported['id']}/history",
            headers=auth, params={"trackId": "t0", "passageStartMeasure": 1, "passageEndMeasure": 1},
        )
        assert response.json() == {"baselineAttemptId": None, "items": []}

    def test_corrections_only_for_private_attempts(self, client, seeded, auth, post_attempt):
        attempt = post_attempt(auth, _exercise_attempt(seeded[TRIADS])).json()
        response = client.post(
            f"/api/v1/practice-attempts/{attempt['id']}/corrections",
            headers=auth, json={"subject": "INTERVENTION", "isHelpful": True},
        )
        assert response.status_code == 404

    @pytest.mark.parametrize(
        ("overrides", "status"),
        [
            ({"consent": False}, 400),
            ({"scoreImportId": "missing"}, 404),
            ({"attemptId": "missing"}, 404),
            ({"eventType": "SOMETHING"}, 422),
            ({"passageStartMeasure": 0}, 422),
        ],
    )
    def test_telemetry_validation(self, client, auth, imported, overrides, status):
        body = {"consent": True, "scoreImportId": imported["id"], "eventType": "RETURNED", **overrides}
        assert client.post("/api/v1/practice-attempts/telemetry", headers=auth, json=body).status_code == status

    def test_telemetry_rejects_attempt_from_other_import(self, client, auth, create_import, imported, post_attempt):
        attempt = post_attempt(auth, _private_attempt(imported)).json()
        other = create_import(auth, title="Other")
        body = {"consent": True, "scoreImportId": other["id"], "attemptId": attempt["id"], "eventType": "RETURNED"}
        assert client.post("/api/v1/practice-attempts/telemetry", headers=auth, json=body).status_code == 404
