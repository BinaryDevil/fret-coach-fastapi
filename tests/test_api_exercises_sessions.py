from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

CHROMATIC = "chromatic-1-2-3-4"
PICKING = "alternate-picking-single-string"


class TestExercises:
    def test_list_is_public_and_ordered(self, client, seeded):
        response = client.get("/api/v1/exercises")
        assert response.status_code == 200
        items = response.json()
        assert [item["libraryOrder"] for item in items] == [1, 2, 3]
        first = items[0]
        assert first["slug"] == CHROMATIC
        assert first["configuration"]["bpmRange"] == [40, 140]
        assert first["score"]["defaultBpm"] == 60
        assert "timeline" not in first["score"] and "content" not in first["score"]
        assert first["loopRegions"][0]["title"] == "Full Loop"

    @pytest.mark.parametrize(
        ("query", "count"),
        [("type=FINGER_DEXTERITY", 2), ("type=CHROMATIC", 1), ("difficulty=INTERMEDIATE", 0), ("difficulty=BEGINNER", 3)],
    )
    def test_list_filters(self, client, seeded, query, count):
        assert len(client.get(f"/api/v1/exercises?{query}").json()) == count

    def test_list_rejects_unknown_filter_value(self, client, seeded):
        assert client.get("/api/v1/exercises?type=JAZZ").status_code == 422

    def test_detail_includes_timeline_and_revision(self, client, seeded):
        exercise = seeded[CHROMATIC]
        score = exercise["score"]
        assert score["revision"] == f"v{score['eventVersion']}:{score['eventSourceHash']}"
        assert score["timeline"]["totalMeasures"] == 12
        assert len(score["timeline"]["events"]) == 48
        assert exercise["loopRegions"] == [
            {"id": exercise["loopRegions"][0]["id"], "title": "Full Loop", "startMeasure": 1, "endMeasure": 12, "defaultBpm": 60}
        ]

    def test_detail_by_slug_matches_by_id(self, client, seeded):
        exercise = seeded[PICKING]
        assert client.get(f"/api/v1/exercises/slug/{PICKING}").json() == exercise

    @pytest.mark.parametrize("path", ["/api/v1/exercises/missing", "/api/v1/exercises/slug/missing"])
    def test_detail_not_found(self, client, seeded, path):
        assert client.get(path).status_code == 404

    def test_unpublished_exercise_is_hidden(self, client, seeded, db):
        exercise = seeded[CHROMATIC]
        db.execute("UPDATE exercises SET is_published = 0 WHERE id = ?", (exercise["id"],))
        db.commit()
        assert client.get(f"/api/v1/exercises/{exercise['id']}").status_code == 404
        assert len(client.get("/api/v1/exercises").json()) == 2

    def test_progress_requires_auth_and_defaults_to_null(self, client, seeded, auth):
        exercise_id = seeded[CHROMATIC]["id"]
        assert client.get(f"/api/v1/exercises/{exercise_id}/progress").status_code == 401
        response = client.get(f"/api/v1/exercises/{exercise_id}/progress", headers=auth)
        assert response.status_code == 200
        assert response.json() is None


class TestDashboard:
    def test_new_user_dashboard(self, client, seeded, auth):
        body = client.get("/api/v1/dashboard/me", headers=auth).json()
        assert body["stats"] == {"currentDayStreak": 0, "averageAccuracyPercent": 0, "playedThisWeekMs": 0}
        assert body["nextPractice"]["slug"] == CHROMATIC
        assert body["nextPractice"]["targetBpm"] == 60
        assert [card["rank"] for card in body["recommendations"]] == [1, 2, 3]
        assert body["coachNote"].startswith("Start with a clean")

    def test_empty_library(self, client, auth):
        body = client.get("/api/v1/dashboard/me", headers=auth).json()
        assert body["nextPractice"] is None
        assert body["recommendations"] == []


def _start_session(client, headers, exercise, **extra):
    return client.post("/api/v1/practice-sessions", headers=headers, json={"exerciseId": exercise["id"], **extra})


class TestPracticeSessions:
    def test_create_uses_score_default_bpm(self, client, seeded, auth):
        response = _start_session(client, auth, seeded[CHROMATIC])
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "IN_PROGRESS"
        assert body["targetBpm"] == 60
        assert body["exercise"]["slug"] == CHROMATIC
        assert body["loopRegion"] is None

    def test_create_with_loop_and_explicit_bpm(self, client, seeded, auth, db):
        exercise = seeded[CHROMATIC]
        loop = exercise["loopRegions"][0]
        db.execute("UPDATE loop_regions SET default_bpm = 70 WHERE id = ?", (loop["id"],))
        db.commit()
        assert _start_session(client, auth, exercise, loopRegionId=loop["id"]).json()["targetBpm"] == 70
        assert _start_session(client, auth, exercise, loopRegionId=loop["id"], targetBpm=90).json()["targetBpm"] == 90

    def test_rejects_loop_from_another_exercise(self, client, seeded, auth):
        other_loop = seeded[PICKING]["loopRegions"][0]["id"]
        response = _start_session(client, auth, seeded[CHROMATIC], loopRegionId=other_loop)
        assert response.status_code == 400

    def test_unknown_exercise(self, client, seeded, auth):
        assert client.post("/api/v1/practice-sessions", headers=auth, json={"exerciseId": "nope"}).status_code == 404

    def test_target_bpm_bounds(self, client, seeded, auth):
        assert _start_session(client, auth, seeded[CHROMATIC], targetBpm=10).status_code == 422

    def test_recording_and_completion(self, client, seeded, auth):
        session = _start_session(client, auth, seeded[CHROMATIC]).json()
        recording = client.post(
            f"/api/v1/practice-sessions/{session['id']}/recordings",
            headers=auth,
            json={"storageKey": "rec/1.webm", "mimeType": "audio/webm", "durationMs": 1500},
        )
        assert recording.status_code == 200
        assert recording.json()["sessionId"] == session["id"]

        completed = client.post(f"/api/v1/practice-sessions/{session['id']}/complete", headers=auth)
        assert completed.status_code == 200
        assert completed.json()["status"] == "COMPLETED"
        assert completed.json()["durationSeconds"] >= 0

        progress = client.get("/api/v1/progress/me", headers=auth).json()
        assert progress["totalSessions"] == 1
        assert progress["currentDayStreak"] == 1
        assert progress["longestDayStreak"] == 1

        # A completed session no longer accepts recordings or completion.
        assert client.post(f"/api/v1/practice-sessions/{session['id']}/complete", headers=auth).status_code == 404
        assert client.post(
            f"/api/v1/practice-sessions/{session['id']}/recordings",
            headers=auth,
            json={"storageKey": "rec/2.webm", "mimeType": "audio/webm"},
        ).status_code == 404

    def test_sessions_are_private(self, client, seeded, auth, register):
        session = _start_session(client, auth, seeded[CHROMATIC]).json()
        intruder = register()["headers"]
        assert client.post(f"/api/v1/practice-sessions/{session['id']}/complete", headers=intruder).status_code == 404


class TestStreaks:
    def _complete_with_last_practice(self, client, seeded, auth, db, user, days_ago: int | None, streak: int = 3):
        last = None
        if days_ago is not None:
            day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
            last = day.isoformat().replace("+00:00", "Z")
        db.execute(
            "UPDATE user_progress SET current_day_streak = ?, longest_day_streak = 5, last_practiced_on = ? WHERE user_id = ?",
            (streak, last, user["user"]["id"]),
        )
        db.commit()
        session = _start_session(client, auth, seeded[CHROMATIC]).json()
        client.post(f"/api/v1/practice-sessions/{session['id']}/complete", headers=auth)
        return client.get("/api/v1/progress/me", headers=auth).json()

    @pytest.mark.parametrize(("days_ago", "expected"), [(0, 3), (1, 4), (2, 1), (None, 1)])
    def test_streak_rules(self, client, seeded, auth, db, user, days_ago, expected):
        progress = self._complete_with_last_practice(client, seeded, auth, db, user, days_ago)
        assert progress["currentDayStreak"] == expected
        assert progress["longestDayStreak"] == 5

    def test_longest_streak_grows(self, client, seeded, auth, db, user):
        progress = self._complete_with_last_practice(client, seeded, auth, db, user, 1, streak=5)
        assert progress["currentDayStreak"] == 6
        assert progress["longestDayStreak"] == 6

    def test_progress_row_is_recreated_if_missing(self, client, seeded, auth, db, user):
        db.execute("DELETE FROM user_progress WHERE user_id = ?", (user["user"]["id"],))
        db.commit()
        session = _start_session(client, auth, seeded[CHROMATIC]).json()
        client.post(f"/api/v1/practice-sessions/{session['id']}/complete", headers=auth)
        assert client.get("/api/v1/progress/me", headers=auth).json()["totalSessions"] == 1


class TestAnalysis:
    @pytest.fixture
    def recording(self, client, seeded, auth):
        session = _start_session(client, auth, seeded[CHROMATIC]).json()
        return client.post(
            f"/api/v1/practice-sessions/{session['id']}/recordings",
            headers=auth,
            json={"storageKey": "rec/a.webm", "mimeType": "audio/webm"},
        ).json()

    def test_queue_is_idempotent(self, client, auth, recording):
        url = f"/api/v1/recordings/{recording['id']}/analysis"
        assert client.get(url, headers=auth).json() == {"status": "NOT_REQUESTED"}
        first = client.post(url, headers=auth).json()
        assert first["status"] == "QUEUED"
        assert client.post(url, headers=auth).json()["id"] == first["id"]
        detail = client.get(url, headers=auth).json()
        assert detail["status"] == "QUEUED"
        assert detail["feedback"] == []

    def test_other_users_cannot_see_recording(self, client, register, recording):
        intruder = register()["headers"]
        url = f"/api/v1/recordings/{recording['id']}/analysis"
        assert client.post(url, headers=intruder).status_code == 404
        assert client.get(url, headers=intruder).status_code == 404
